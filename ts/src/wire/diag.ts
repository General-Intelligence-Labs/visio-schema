/**
 * Pull a device's diagnostic log over the Visio bus. Owns no connection.
 *
 * The TypeScript twin of `visio_schema/wire/diag.py`, and the receiver-side
 * counterpart of `wire/ota` — same shape, pointed device->host.
 *
 * The caller is a **blind relay**, as it is for OTA in the other direction: a
 * `.vdlg` is ciphertext under a fleet key the caller never holds, and this
 * carries the bytes unchanged. Decoding happens on a machine with the key.
 *
 * Transport-agnostic on purpose: `listFiles`/`readFile` take an injected
 * `DiagIo` — `send(raw DiagRequest)` on CONTROL_STREAM_DIAG, and
 * `recv(timeout) -> raw DiagReply | null` off the device's `/<device>/diag`
 * channel — so one state machine drives a socket, a serial port or a bus leg.
 * FINDING that channel, and building the leg, is host policy and stays with
 * the caller; the contract repo owns the protocol and nothing else. (Python
 * draws the same line: `ota_bus_leg.py` lives in visio-embedded, not here.)
 *
 * Async, for the reason `wire/ota` is: a synchronous `recv(timeout)` is
 * unimplementable in JS. The protocol is `service/diag/diag.proto`; the
 * contract is docs/protocol/diag_log.md.
 */
import { create, fromBinary, toBinary } from '@bufbuild/protobuf';

import {
  DiagAbortSchema,
  DiagListSchema,
  DiagReadSchema,
  DiagReplySchema,
  DiagRequestSchema,
  DiagStatus_State,
  type DiagReply,
} from '../gen/visio_schema/v1/service/diag/diag_pb.js';

/**
 * Every request gets its own session id, counted up from here, and the device
 * echoes it on every reply. Per REQUEST, not per connection: a read ends for
 * the caller at its last chunk while the device is still sending a trailing
 * DONE, and under a shared id that DONE would end the NEXT read empty.
 */
export const DEFAULT_SESSION_ID = 0xd1a6n;

/**
 * The device paces chunks (16 KiB every ~20 ms by default), so a gap this long
 * between replies means the read is dead rather than slow.
 */
export const STALL_TIMEOUT_S = 10.0;

let counter = DEFAULT_SESSION_ID;
/** The next per-request session id. */
export function nextSessionId(): bigint {
  const id = counter;
  counter += 1n;
  return id;
}

/** The device declined or failed a request; `code` is DiagStatus.error_code. */
export class DiagError extends Error {
  readonly code: string;
  constructor(code: string, message = '') {
    super(message || code);
    this.name = 'DiagError';
    this.code = code;
  }
}

export interface FileEntry {
  readonly name: string;
  readonly size: bigint;
  readonly bytesValid: bigint;
  /** 1 = flash, 2 = card. */
  readonly tier: number;
}

/** A `.vdlg` is a log; anything else in the catalogue is not. */
export const isLog = (f: FileEntry): boolean => f.name.endsWith('.vdlg');

/** The send/recv pair this driver rides. Times are SECONDS, as in `wire/ota`. */
export interface DiagIo {
  /** One serialized DiagRequest onto the device's diag stream. false = link gone. */
  send(payload: Uint8Array): boolean | Promise<boolean>;
  /**
   * The next DiagReply payload, or null if none arrived within `timeoutS`.
   * A timeout of 0 must return promptly with whatever is already buffered,
   * and a non-zero one must CONSUME that much time when nothing arrives —
   * the reply loop below is otherwise a spin.
   *
   * The bytes must be the caller's to keep until `recv` is next called; this
   * driver copies anything it retains beyond that.
   */
  recv(timeoutS: number): Promise<Uint8Array | null>;
  /** Monotonic SECONDS. Injected so a test can move time without waiting. */
  clock(): number;
}

export interface DiagOptions {
  sessionId?: bigint;
  /** Required on a shared bus leg; on a dedicated link the link IS the address. */
  targetDevice?: string;
  timeoutS?: number;
  onProgress?: (bytes: number) => void;
}

type Addr = { sessionId: bigint; targetDevice: string };

function request(a: Addr, body: 'list' | 'abort'): Uint8Array;
function request(a: Addr, body: 'read', read: { name: string; offset: bigint; len: number }): Uint8Array;
function request(
  a: Addr,
  body: 'list' | 'abort' | 'read',
  read?: { name: string; offset: bigint; len: number },
): Uint8Array {
  const m = create(DiagRequestSchema, { sessionId: a.sessionId });
  // Only when addressed: on a shared bus leg a hub cannot tell whom the
  // request was meant for, and on a dedicated link an empty value is right.
  if (a.targetDevice) m.targetDevice = a.targetDevice;
  // Each arm names its schema: an empty body still has to encode as the
  // oneof arm it is, not as an unset oneof the device would ignore.
  if (body === 'read') m.body = { case: 'read', value: create(DiagReadSchema, read) };
  else if (body === 'list') m.body = { case: 'list', value: create(DiagListSchema, {}) };
  else m.body = { case: 'abort', value: create(DiagAbortSchema, {}) };
  return toBinary(DiagRequestSchema, m);
}

const addr = (o: DiagOptions): Addr => ({
  sessionId: o.sessionId ?? nextSessionId(),
  targetDevice: o.targetDevice ?? '',
});

export const listMessage = (o: DiagOptions = {}): Uint8Array => request(addr(o), 'list');
export const abortMessage = (o: DiagOptions = {}): Uint8Array => request(addr(o), 'abort');
export const readMessage = (
  name: string,
  offset: bigint = 0n,
  length = 0,
  o: DiagOptions = {},
): Uint8Array => request(addr(o), 'read', { name, offset, len: length });

/**
 * A refused send is a dead link, and saying so beats waiting out the full stall
 * window and reporting a `timeout` — a different cause, and a slower answer.
 * `wire/ota` acts on the same signal; the two twins should not disagree.
 */
async function sendOrThrow(io: DiagIo, payload: Uint8Array): Promise<void> {
  if (!(await io.send(payload))) throw new DiagError('link', 'the link refused the request');
}

/**
 * Yield this session's replies until the device goes quiet for `timeoutS`.
 *
 * The deadline is reset by every reply that IS ours, so a long healthy read
 * never trips it, while a dead one trips after one stall window. Replies from
 * another session on a shared channel are skipped without resetting it.
 */
async function* replies(
  io: DiagIo,
  sessionId: bigint,
  timeoutS: number,
  startedAt: number,
): AsyncGenerator<DiagReply> {
  // From when the request was SENT, not from when an awaited send resolved.
  // `send` may yield, and a caller whose clock steps rather than ticks would
  // otherwise start the window after time had already moved — leaving a stall
  // that can never expire.
  let deadline = startedAt + timeoutS;
  for (;;) {
    const raw = await io.recv(Math.max(0, deadline - io.clock()));
    if (raw === null) {
      if (io.clock() >= deadline) {
        throw new DiagError('timeout', `no reply from the device for ${timeoutS.toFixed(0)} s`);
      }
      continue;
    }
    let r: DiagReply;
    try {
      r = fromBinary(DiagReplySchema, raw);
    } catch (e) {
      // FATAL, not skipped. The caller hands us frames already filtered to the
      // device's diag channel, so a frame that will not decode is schema drift
      // — a real failure, not a silence to wait out. Python agrees by omission:
      // its `ParseFromString` raises and nothing catches it.
      throw new DiagError('decode', String(e));
    }
    // A different session on a shared channel IS someone else's, and is
    // skipped without resetting the stall deadline.
    if (r.sessionId !== sessionId) continue;
    deadline = io.clock() + timeoutS;
    yield r;
  }
}

/** Ask for the catalogue. Flash tier first, then card, oldest file first. */
export async function listFiles(io: DiagIo, o: DiagOptions = {}): Promise<FileEntry[]> {
  const a = addr(o);
  const timeoutS = o.timeoutS ?? STALL_TIMEOUT_S;
  const startedAt = io.clock();
  await sendOrThrow(io, request(a, 'list'));
  for await (const r of replies(io, a.sessionId, timeoutS, startedAt)) {
    if (r.body.case === 'listing') {
      return r.body.value.files.map((f) => ({
        name: f.name,
        size: f.size,
        bytesValid: f.bytesValid,
        tier: f.tier,
      }));
    }
    if (r.body.case === 'status') {
      const s = r.body.value;
      if (s.errorCode || s.state === DiagStatus_State.FAILED) {
        throw new DiagError(s.errorCode || 'failed', s.errorMessage);
      }
    }
  }
  throw new Error('unreachable');
}

/**
 * Read `[offset, offset+length)` of one file; `length === 0` reads to the end.
 *
 * Returns the raw bytes as they sit on the device — for a `.vdlg` that is
 * header plus ciphertext. Chunks arrive in order; a gap (a chunk whose offset
 * is not the next expected byte) is a protocol failure and is NOT papered
 * over, because a spliced ciphertext decrypts to garbage from the gap on.
 */
export async function readFile(
  io: DiagIo,
  name: string,
  o: DiagOptions & { offset?: bigint; length?: number } = {},
): Promise<Uint8Array> {
  const a = addr(o);
  const timeoutS = o.timeoutS ?? STALL_TIMEOUT_S;
  const offset = o.offset ?? 0n;
  const startedAt = io.clock();
  await sendOrThrow(io, request(a, 'read', { name, offset, len: o.length ?? 0 }));

  const parts: Uint8Array[] = [];
  let total = 0;
  let expect = offset;
  const joined = () => {
    const out = new Uint8Array(total);
    let at = 0;
    for (const p of parts) { out.set(p, at); at += p.length; }
    return out;
  };

  for await (const r of replies(io, a.sessionId, timeoutS, startedAt)) {
    if (r.body.case === 'chunk') {
      const c = r.body.value;
      if (c.name !== name || c.offset !== expect) {
        // Our session, but not our bytes: a device bug. Splicing around it
        // would yield ciphertext that decrypts to garbage.
        try {
          await io.send(request(a, 'abort'));
        } catch {
          // The link is already gone; the gap is still what we report.
        }
        throw new DiagError('gap', `chunk ${JSON.stringify(c.name)}@${c.offset}, expected ${JSON.stringify(name)}@${expect}`);
      }
      // COPY. protobuf-es hands back a zero-copy subarray of the buffer
      // `recv` returned, so retaining it makes the result depend on that
      // buffer still being intact at the END of the transfer. A `DiagIo` that
      // reuses a scratch buffer — the natural shape for a serial or socket
      // reader — would then yield silently corrupt bytes, and for a .vdlg
      // that is ciphertext garbage nothing can detect. Python copies here
      // too, for free: `out += c.data` on immutable bytes.
      parts.push(c.data.slice());
      total += c.data.length;
      expect += BigInt(c.data.length);
      o.onProgress?.(total);
      if (c.last) return joined();
    } else if (r.body.case === 'status') {
      const s = r.body.value;
      if (s.state === DiagStatus_State.DONE) return joined();
      if (s.errorCode || s.state === DiagStatus_State.FAILED) {
        throw new DiagError(s.errorCode || 'failed', s.errorMessage);
      }
    }
  }
  throw new Error('unreachable');
}
