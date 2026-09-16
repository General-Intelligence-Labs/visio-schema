/**
 * wire/ota — the OTA driver, TypeScript.
 *
 * The third implementation of one contract, beside `visio_schema/wire/ota.py`
 * and `visio_schema/wire/ota.hpp`. The normative spec is
 * `docs/protocol/ota.md`; this is the state machine and the message shapes.
 *
 * IT IS ASYNC, AND THAT IS NOT A STYLE CHOICE. A synchronous `recv(timeout)`
 * cannot exist in JavaScript — there is no way to block. Both the vector
 * generator and ota.md's Conformance section already refuse to pin the recv
 * CALL PATTERN for exactly this reason: the Python reference drains with
 * `recv(0.0)` between frames, which an async driver cannot, so pinning it would
 * make one of the three unwritable. What IS pinned is the sends and the
 * outcome, and those this matches byte for byte.
 *
 * Shaped after the C++ twin rather than the Python one: the seams go in `Io`
 * and the tuning in `Options`, because Python's 22 flat keyword arguments do
 * not survive translation into a language without them. Times are SECONDS
 * throughout, as in both twins, so a vector's params transfer unchanged.
 */
import { create, fromBinary, toBinary } from '@bufbuild/protobuf';

import {
  OtaMessageSchema,
  OtaStatusSchema,
  OtaStatus_State,
  type OtaStatus,
} from '../gen/visio_schema/v1/service/ota/ota_pb';

// ── the contract's constants ────────────────────────────────────────────────

/** `CONTROL_STREAM_OTA`. Read off the generated enum rather than restated. */
export const STREAM_OTA = 5;

export const VENC_MAGIC = new Uint8Array([0x56, 0x45, 0x4e, 0x43]); // "VENC"
export const RKFW_MAGIC = new Uint8Array([0x52, 0x4b, 0x46, 0x57]); // "RKFW"

export const DEFAULT_SESSION_ID = 0xca11n;
/** The reserved session a head publishes a rig's whole-transaction verdict on. */
export const RIG_TERMINAL_SESSION = 0xb1dn;

export const TCP_WINDOW_BYTES = 2 * 1024 * 1024;
export const TCP_CHUNK_BYTES = 32 * 1024;
/**
 * The largest `OtaChunk.data` any device can decode, whatever it advertises.
 * nanopb is built without PB_FIELD_32BIT, so `pb_size_t` is 16-bit and
 * PB_SIZE_MAX is 65535; a larger chunk cannot be pb_decode'd at all and the
 * device answers every frame with a decode failure, which reads like a dead
 * link rather than a size problem. Headroom left for the wrapping fields.
 */
export const MAX_CHUNK_BYTES = 60 * 1024;
/** Bounds what a DEVICE may talk us into, never what the caller asked for. */
export const MIN_CHUNK_BYTES = 8 * 1024;
/** CDC-ACM: total in-flight must stay inside the gadget RX FIFO. */
export const USB_WINDOW_BYTES = 8 * 1024;
export const USB_CHUNK_BYTES = 4 * 1024;

export const QUERY_WAIT_S = 1.5;
export const STALL_TIMEOUT_S = 45.0;
export const SOFT_RETRY_S = 6.0;
export const COMMIT_WAIT_S = 8.0;
export const DEADLINE_S = 900.0;

/**
 * What a pusher drops on its own link for the duration of a transfer.
 *
 * Video alone, because video alone binds. Why this lives with the driver, and
 * why every hop must quiet its OWN leg: docs/protocol/ota.md §6.
 */
export const QUIESCE_RULES = ['**/camera/*'] as const;
export const QUIESCE_COMMAND_ID = 0xb15;

/**
 * Why a relay ended, as a CLOSED set.
 *
 * `detail` is prose for humans and free to be reworded; this is the half the
 * conformance vectors pin, because three implementations cannot be held to a
 * sentence. Values are wire-visible and APPEND-ONLY: never renumber, never
 * reuse. 10/11/12 are retired, not free.
 */
export enum Reason {
  OkStaged = 0,
  OkSuccess = 1,
  OkStagedNoTransfer = 2,
  OkCommittedUnconfirmed = 3,
  OkLinkAfterCommit = 4,
  FailDevice = 5,
  FailStalled = 6,
  FailDeadline = 7,
  FailTooLossy = 8,
  FailLinkDropped = 9,
  OkUploadOnly = 13,
}

export interface Progress {
  readonly sent: number;
  readonly acked: number;
  readonly total: number;
  readonly resumes: number;
  readonly state: string;
}

export interface Outcome {
  readonly ok: boolean;
  readonly reason: Reason;
  readonly detail: string;
  readonly acked: number;
  readonly total: number;
  readonly resumes: number;
}

/**
 * The transport seam. Mirrors the C++ `Io` member for member.
 *
 * NOTHING here knows about a bus, a socket or a native bridge — a caller
 * supplies four functions and the driver supplies the rules.
 */
export interface OtaIo {
  /** One serialized OtaMessage onto the device's OTA stream. false = link gone. */
  send(payload: Uint8Array): boolean | Promise<boolean>;
  /**
   * The next OtaStatus payload, or null if none arrived within `timeoutS`.
   * A timeout of 0 must return promptly with whatever is already buffered.
   */
  recv(timeoutS: number): Promise<Uint8Array | null>;
  /** Monotonic seconds. Injected so a test can move time without waiting. */
  clock(): number;
  /** Random access to the image: a rig package is hundreds of MB. */
  readImage(offset: number, length: number): Uint8Array | Promise<Uint8Array>;
  readonly imageBytes: number;
  /**
   * Drop QUIESCE_RULES on this link for the transfer and put it back after.
   * Called `true` before the begin and `false` on EVERY exit; returns whether
   * the device acked. Unset = push against a live link. Contract: ota.md §6.
   */
  quiesce?(quiet: boolean): boolean | Promise<boolean>;
  onProgress?(p: Progress): void;
}

export interface OtaOptions {
  fwVersion: string;
  /** The IMAGE's board, never the device's — the unit compares it to its own. */
  board?: string;
  targetDevice?: string;
  sessionId?: bigint;
  window?: number;
  chunk?: number;
  /** The LINK's limit, which the device cannot see and never advertises. */
  chunkCap?: number;
  negotiate?: boolean;
  commit?: boolean;
  queryWaitS?: number;
  stallTimeoutS?: number;
  softRetryS?: number;
  commitWaitS?: number;
  deadlineS?: number;
  maxResumes?: number;
}

// ── message encoding ────────────────────────────────────────────────────────

type Addr = Required<Pick<OtaOptions, 'targetDevice' | 'sessionId'>>;
type MessageBody = NonNullable<Parameters<typeof create<typeof OtaMessageSchema>>[1]>['body'];

function message(o: Addr, body: MessageBody): Uint8Array {
  return toBinary(
    OtaMessageSchema,
    create(OtaMessageSchema, {
      targetDevice: o.targetDevice,
      sessionId: o.sessionId,
      body,
    }),
  );
}

export function beginMessage(
  a: Addr,
  totalBytes: number,
  chunkBytes: number,
  fwVersion: string,
  board = '',
): Uint8Array {
  return message(a, {
    case: 'begin',
    value: { fwVersion, board, totalBytes: BigInt(totalBytes), chunkBytes },
  });
}

export function chunkMessage(a: Addr, offset: number, data: Uint8Array): Uint8Array {
  return message(a, { case: 'chunk', value: { offset: BigInt(offset), data } });
}

export function commitMessage(a: Addr): Uint8Array {
  return message(a, { case: 'commit', value: {} });
}

export function abortMessage(a: Addr, reason: string): Uint8Array {
  return message(a, { case: 'abort', value: { reason } });
}

export function queryMessage(a: Addr): Uint8Array {
  return message(a, { case: 'query', value: {} });
}

export function decodeStatus(raw: Uint8Array): OtaStatus {
  return fromBinary(OtaStatusSchema, raw);
}

/**
 * A session id for one push. Counter-backed, clock-seeded: two pushes started
 * in the same millisecond must never share one, and a board can still be
 * holding the previous run's session across an app restart.
 */
let lastSessionId = BigInt(Date.now() % 1_000_000) * 16n;
export function nextSessionId(): bigint {
  lastSessionId += 1n;
  return lastSessionId;
}

/** Why `raw` is not an OTA bundle, or null if it is one. */
export function bundleError(raw: Uint8Array): string | null {
  const starts = (m: Uint8Array) =>
    raw.length >= m.length && m.every((b, i) => raw[i] === b);
  if (starts(VENC_MAGIC)) return null;
  // A multi-board package is a stored ustar; "ustar" sits at offset 257.
  if (raw.length > 262) {
    const magic = String.fromCharCode(...raw.subarray(257, 262));
    if (magic === 'ustar') return null;
  }
  if (starts(RKFW_MAGIC)) {
    return (
      'a raw RKFW update.img, not an OTA bundle — wrap it first with ' +
      'scripts/ota_release.py (--image … --version … --board … --bundle-key …)'
    );
  }
  return `not a VENC OTA bundle (magic ${JSON.stringify(
    String.fromCharCode(...raw.subarray(0, 4)),
  )})`;
}

/**
 * The chunk size to declare in OtaBegin and then actually send.
 *
 * The four rules, in the order wire/ota.py's `negotiate_chunk` states them:
 * no advert (0) is firmware predating the field and leaves the caller's own
 * default UNTOUCHED; an advert WINS, up or down, because min(ours, theirs)
 * would make the field a no-op for every board that can take more; the advert
 * is bounded by [MIN_CHUNK_BYTES, MAX_CHUNK_BYTES]; and `cap` applies LAST and
 * to everything, being the LINK's limit rather than the device's.
 */
export function negotiatedChunkBytes(
  advertised: number,
  { want = TCP_CHUNK_BYTES, cap }: { want?: number; cap?: number } = {},
): number {
  const negotiated =
    advertised > 0
      ? Math.max(MIN_CHUNK_BYTES, Math.min(advertised, MAX_CHUNK_BYTES))
      : want;
  // A non-positive cap is no cap. Letting a 0 through would make the chunk size
  // 0, and that does not fail loudly — the send loop never advances.
  return cap !== undefined && cap > 0 ? Math.min(negotiated, cap) : negotiated;
}

// ── the driver ──────────────────────────────────────────────────────────────

interface Folded {
  acked: number;
  staged: boolean;
  succeeded: boolean;
  failure: string | null;
  resumeTo: number | null;
  advert: number;
}

function fold(st: Folded, raw: Uint8Array, sessionId: bigint): void {
  const s = decodeStatus(raw);
  // A status stamped for some OTHER session is not ours to fold: its
  // bytes_received would move our watermark and its FAILED would abort a
  // healthy transfer. Unset (0) IS folded — the device leaves it clear on the
  // periodic no-session IDLE, and older firmware never sets it.
  if (s.sessionId !== 0n && s.sessionId !== sessionId) return;
  if (s.maxChunkBytes > 0) {
    st.advert = st.advert === 0 ? s.maxChunkBytes : Math.min(st.advert, s.maxChunkBytes);
  }
  const acked = Number(s.bytesReceived);
  if (s.state === OtaStatus_State.STAGED) {
    st.staged = true;
    st.acked = Math.max(st.acked, acked);
  } else if (s.state === OtaStatus_State.SUCCESS) {
    st.succeeded = true;
  } else if (s.state === OtaStatus_State.NEEDS_RESUME) {
    if (!st.staged && !st.succeeded) st.resumeTo = acked;
  } else if (s.errorCode || s.state === OtaStatus_State.FAILED) {
    // After staging the device has no session while it reboots, so the last
    // in-flight chunks bounce back as `no_session` FAILEDs — ignore ONLY
    // those; any other post-stage error (revert_failed) is real.
    const benign = !s.errorCode || s.errorCode === 'no_session';
    if (!((st.staged || st.succeeded) && benign)) {
      st.failure = s.errorMessage || s.errorCode || 'device reported FAILED';
    }
  } else {
    st.acked = Math.max(st.acked, acked);
  }
}

/** Send the pre-Begin OtaQuery and collect adverts until `queryWaitS`. */
export async function negotiateChunk(io: OtaIo, o: OtaOptions): Promise<number> {
  const a: Addr = {
    targetDevice: o.targetDevice ?? '',
    sessionId: o.sessionId ?? DEFAULT_SESSION_ID,
  };
  const want = o.chunk ?? TCP_CHUNK_BYTES;
  if (!(await io.send(queryMessage(a)))) return negotiatedChunkBytes(0, { want, cap: o.chunkCap });

  const st: Folded = {
    acked: 0, staged: false, succeeded: false, failure: null, resumeTo: null, advert: 0,
  };
  const deadline = io.clock() + (o.queryWaitS ?? QUERY_WAIT_S);
  for (;;) {
    const left = deadline - io.clock();
    if (left <= 0) break;
    const raw = await io.recv(left);
    if (raw === null) break;
    fold(st, raw, a.sessionId);
  }
  // The SMALLEST advert across responders: on a hub every leaf answers, and the
  // chunk has to fit the one with the tightest buffer.
  return negotiatedChunkBytes(st.advert, { want, cap: o.chunkCap });
}

/**
 * Stream one image to one device: begin -> windowed, RESUME-aware chunks ->
 * commit, paced by `OtaStatus.bytes_received`.
 *
 * `ok` is true on STAGED/SUCCESS **or** a post-commit link drop — the device
 * reboots to apply and its STAGED ack routinely races that drop.
 */
export async function relay(io: OtaIo, o: OtaOptions): Promise<Outcome> {
  const a: Addr = {
    targetDevice: o.targetDevice ?? '',
    sessionId: o.sessionId ?? DEFAULT_SESSION_ID,
  };
  const total = io.imageBytes;
  const windowBytes = o.window ?? TCP_WINDOW_BYTES;
  const stallTimeoutS = o.stallTimeoutS ?? STALL_TIMEOUT_S;
  const softRetryS = o.softRetryS ?? SOFT_RETRY_S;
  const commitWaitS = o.commitWaitS ?? COMMIT_WAIT_S;
  const deadlineS = o.deadlineS ?? DEADLINE_S;

  const st: Folded = {
    acked: 0, staged: false, succeeded: false, failure: null, resumeTo: null, advert: 0,
  };
  let resumes = 0;
  let committed = false;
  let linkGone = false;
  let quieted = false;

  const emit = (sent: number, state: string) =>
    io.onProgress?.({ sent, acked: st.acked, total, resumes, state });

  const pump = async (timeoutS: number) => {
    const raw = await io.recv(timeoutS);
    if (raw !== null) fold(st, raw, a.sessionId);
  };

  const sendFrame = async (frame: Uint8Array): Promise<boolean> => {
    if (await io.send(frame)) return true;
    linkGone = true;
    return false;
  };

  const abort = async (why: string) => {
    try {
      await io.send(abortMessage(a, why));
    } catch {
      /* best effort: the link is already the problem */
    }
  };

  const done = (ok: boolean, detail: string, reason: Reason): Outcome => ({
    ok, reason, detail, acked: st.acked, total, resumes,
  });

  try {
    if (io.quiesce) {
      // Before the negotiation, not after: the OtaQuery's answer crosses the
      // same link the video is saturating, and a query that times out silently
      // costs the transfer its negotiated chunk size.
      quieted = await io.quiesce(true);
    }

    const chunk =
      o.negotiate === false
        ? negotiatedChunkBytes(0, { want: o.chunk ?? TCP_CHUNK_BYTES, cap: o.chunkCap })
        : await negotiateChunk(io, o);

    const maxResumes = o.maxResumes ?? 2 * (Math.floor(total / Math.max(chunk, 1)) + 2) + 16;

    if (!(await sendFrame(beginMessage(a, total, chunk, o.fwVersion, o.board ?? '')))) {
      return done(false, 'link dropped before the begin', Reason.FailLinkDropped);
    }

    const tStart = io.clock();
    let lastProgress = tStart;
    let lastSoft = tStart;
    let lastAck = 0;
    let cursor = 0;
    emit(0, 'begin');
    // Read once before streaming: a device whose other slot already holds this
    // build answers STAGED immediately, and a wrong-board refusal comes back
    // just as fast. Both are worth catching before pushing 60 MB.
    await pump(0);

    while (!st.failure && !st.succeeded && !st.staged && !linkGone) {
      if (io.clock() - tStart > deadlineS) {
        await abort('deadline');
        return done(false, `exceeded ${deadlineS.toFixed(0)}s overall deadline`, Reason.FailDeadline);
      }
      if (st.resumeTo !== null) {
        if (++resumes > maxResumes) {
          await abort('too lossy');
          return done(false, `too lossy: ${resumes} resumes`, Reason.FailTooLossy);
        }
        cursor = st.resumeTo;
        st.resumeTo = null; // NOTE: do NOT reset the stall clock
        continue;
      }
      const n = Math.min(chunk, total - cursor);
      // Bound TOTAL in-flight (unacked) to `window`. With a non-blocking
      // transport this is the ONLY backpressure.
      if (cursor < total && cursor - st.acked + n <= windowBytes) {
        const data = await io.readImage(cursor, n);
        if (data.length !== n) return done(false, 'could not read the image', Reason.FailDevice);
        if (!(await sendFrame(chunkMessage(a, cursor, data)))) break;
        cursor += n;
        emit(cursor, 'sending');
        await pump(0);
      } else if (cursor >= total && st.acked >= total) {
        break; // all sent + acked -> commit
      } else {
        await pump(0.05);
      }
      const now = io.clock();
      if (st.acked > lastAck) {
        lastAck = st.acked;
        lastProgress = now;
        lastSoft = now;
      } else if (now - lastProgress > stallTimeoutS) {
        await abort('stall');
        return done(false, 'stalled', Reason.FailStalled);
      } else if (now - lastSoft > softRetryS && st.resumeTo === null && cursor > st.acked) {
        lastSoft = now; // quiet tail -> re-drive from acked
        st.resumeTo = st.acked;
      }
    }

    if (linkGone) return done(false, 'link dropped mid-transfer', Reason.FailLinkDropped);
    if (st.failure) return done(false, st.failure, Reason.FailDevice);
    // A/B instant revert: the build was already in the other slot.
    if (st.staged) return done(true, 'staged (no transfer needed)', Reason.OkStagedNoTransfer);

    if (o.commit === false) {
      // Everything arrived; deliberately do not flash it. Abort rather than
      // hanging up, so the device frees its staging now instead of holding a
      // half-session until the next push collides with it.
      await abort('no-flash');
      return done(true, 'uploaded, not committed', Reason.OkUploadOnly);
    }

    emit(cursor, 'committing');
    if (!(await sendFrame(commitMessage(a)))) {
      return done(false, 'link dropped before the commit', Reason.FailLinkDropped);
    }
    committed = true;

    const commitDeadline = io.clock() + Math.min(stallTimeoutS, commitWaitS);
    while (!st.staged && !st.succeeded && !st.failure && io.clock() < commitDeadline) {
      await pump(0.05);
    }
    if (st.failure) return done(false, st.failure, Reason.FailDevice);
    if (st.succeeded) return done(true, 'SUCCESS', Reason.OkSuccess);
    if (st.staged) return done(true, 'STAGED', Reason.OkStaged);
    // The device reboots to apply and its STAGED ack routinely races the link
    // drop, so DON'T require it — the caller's post-reboot version check is the
    // real proof.
    return done(true, 'committed (staging; STAGED ack raced the reboot)', Reason.OkCommittedUnconfirmed);
  } catch (e) {
    if (st.staged || committed) {
      return done(true, 'link dropped after commit (rebooting to apply)', Reason.OkLinkAfterCommit);
    }
    return done(false, `link dropped mid-transfer: ${String(e)}`, Reason.FailLinkDropped);
  } finally {
    // EVERY exit, including the ones that threw. A device that rebooted to
    // apply will refuse this, and that is fine — it comes back with no policy.
    // A quiesce we never ESTABLISHED is never undone: a policy replaces the
    // link's previous one outright, so restoring one that was never applied
    // would clear whatever the device legitimately had.
    if (quieted) {
      try {
        await io.quiesce?.(false);
      } catch {
        /* the link is gone; there is nothing left to restore it on */
      }
    }
  }
}
