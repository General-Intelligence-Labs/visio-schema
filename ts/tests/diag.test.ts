/**
 * The diag driver's behaviour, which no corpus can see.
 *
 * `tests/golden/diag_vectors.txt` does NOT pin this module — its header says it
 * is generated from `mcap/crypto.py`, and it pins the VDLG/VDLW log containers,
 * a different layer. So unlike `wire/ota`, this port has no cross-language
 * transcript to replay and these cases are authored from the semantics
 * `wire/diag.py` documents as load-bearing. Each one is a rule the Python
 * states in prose and would silently lose if the TypeScript disagreed.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { create, fromBinary, toBinary } from '@bufbuild/protobuf';

import {
  DiagRequestSchema,
  DiagReplySchema,
  DiagStatus_State,
} from '../src/gen/visio_schema/v1/service/diag/diag_pb.js';
import {
  DEFAULT_SESSION_ID,
  DiagError,
  isLog,
  listFiles,
  nextSessionId,
  readFile,
  readMessage,
  abortMessage,
  listMessage,
  type DiagIo,
} from '../src/wire/diag.js';

const SID = 0x51d0n;

// -- fakes ---------------------------------------------------------------- //

type Reply = Uint8Array;

/** A scripted device. `script` is consulted after every send. */
function rig(
  script: (req: ReturnType<typeof decodeReq>, push: (r: Reply) => void) => void,
  opts: { perReplyS?: number } = {},
) {
  const sent: Uint8Array[] = [];
  const queue: Reply[] = [];
  let now = 0;
  let idle = 0;
  const io: DiagIo = {
    send(p) { sent.push(p); script(decodeReq(p), (r) => queue.push(r)); return true; },
    async recv(timeoutS) {
      const r = queue.shift();
      if (r !== undefined) {
        // Delivering a reply COSTS time. Without this the deadline-reset rule
        // is untestable: any number of queued replies arrive at t=0, and a
        // driver that never reset the deadline would still pass.
        now += opts.perReplyS ?? 0;
        return r;
      }
      idle += 1;
      // A fake must never be able to say "nothing, forever" — see above.
      if (idle > 64) throw new Error('the driver never stopped waiting');
      now += timeoutS;          // nothing waiting: the whole window elapses
      return null;
    },
    clock: () => now,
  };
  return { io, sent, queue, at: () => now };
}

const decodeReq = (p: Uint8Array) => fromBinary(DiagRequestSchema, p);

const chunk = (sessionId: bigint, name: string, offset: bigint, data: Uint8Array, last = false) =>
  toBinary(DiagReplySchema, create(DiagReplySchema, {
    sessionId,
    body: { case: 'chunk', value: { name, offset, data, last } },
  }));

const status = (sessionId: bigint, state: DiagStatus_State, errorCode = '', errorMessage = '') =>
  toBinary(DiagReplySchema, create(DiagReplySchema, {
    sessionId,
    body: { case: 'status', value: { state, errorCode, errorMessage } },
  }));

const listing = (sessionId: bigint, files: { name: string; size: bigint; bytesValid: bigint; tier: number }[]) =>
  toBinary(DiagReplySchema, create(DiagReplySchema, {
    sessionId,
    body: { case: 'listing', value: { files } },
  }));

// -- the request encoding ------------------------------------------------- //

test('an empty body still encodes as its oneof arm, not an unset oneof', () => {
  // The precise trap the OTA quiesce hit on the other side: a body with no
  // fields encodes as nothing at all unless the arm is named, and the device
  // then ignores the request.
  for (const [make, arm] of [[listMessage, 'list'], [abortMessage, 'abort']] as const) {
    const r = decodeReq(make({ sessionId: SID }));
    assert.equal(r.body.case, arm);
  }
});

test('target_device is set only when addressed', () => {
  assert.equal(decodeReq(listMessage({ sessionId: SID })).targetDevice, '');
  assert.equal(decodeReq(listMessage({ sessionId: SID, targetDevice: 'GILABS-A' })).targetDevice,
    'GILABS-A');
});

test('a read carries its name, offset and length', () => {
  const r = decodeReq(readMessage('a.vdlg', 4096n, 1024, { sessionId: SID }));
  assert.equal(r.body.case, 'read');
  assert.deepEqual(
    { ...(r.body.case === 'read' ? r.body.value : {}) },
    { $typeName: 'visio_schema.v1.service.diag.DiagRead', name: 'a.vdlg', offset: 4096n, len: 1024 },
  );
});

test('session ids are per REQUEST, counting up from the documented base', () => {
  // A read ends for the caller at its last chunk while the device is still
  // sending a trailing DONE. Under a shared id that DONE ends the NEXT read
  // empty, which is exactly the bug the per-request id exists to prevent.
  const a = nextSessionId();
  const b = nextSessionId();
  assert.notEqual(a, b);
  assert.ok(a >= DEFAULT_SESSION_ID);
});

// -- listing -------------------------------------------------------------- //

test('listFiles returns the catalogue', async () => {
  const r = rig((_req, push) => push(listing(SID, [
    { name: 'a.vdlg', size: 10n, bytesValid: 10n, tier: 1 },
    { name: 'b.vdlw', size: 20n, bytesValid: 5n, tier: 2 },
  ])));
  const files = await listFiles(r.io, { sessionId: SID });
  assert.deepEqual(files.map((f) => f.name), ['a.vdlg', 'b.vdlw']);
  assert.deepEqual(files.map((f) => f.tier), [1, 2]);
  assert.deepEqual(files.map(isLog), [true, false]);
});

test('a FAILED status on the listing raises, carrying the code', async () => {
  const r = rig((_req, push) => push(status(SID, DiagStatus_State.FAILED, 'no_log', 'ring empty')));
  await assert.rejects(() => listFiles(r.io, { sessionId: SID }),
    (e: DiagError) => e.code === 'no_log' && /ring empty/.test(e.message));
});

test('an error_code without FAILED still raises', async () => {
  const r = rig((_req, push) => push(status(SID, DiagStatus_State.READING, 'busy')));
  await assert.rejects(() => listFiles(r.io, { sessionId: SID }),
    (e: DiagError) => e.code === 'busy');
});

// -- reading -------------------------------------------------------------- //

test('chunks are assembled in order and `last` ends the read', async () => {
  const r = rig((_req, push) => {
    push(chunk(SID, 'a.vdlg', 0n, new Uint8Array([1, 2, 3])));
    push(chunk(SID, 'a.vdlg', 3n, new Uint8Array([4, 5]), true));
  });
  const out = await readFile(r.io, 'a.vdlg', { sessionId: SID });
  assert.deepEqual([...out], [1, 2, 3, 4, 5]);
});

test('a DONE status ends the read with what arrived', async () => {
  const r = rig((_req, push) => {
    push(chunk(SID, 'a.vdlg', 0n, new Uint8Array([7])));
    push(status(SID, DiagStatus_State.DONE));
  });
  assert.deepEqual([...await readFile(r.io, 'a.vdlg', { sessionId: SID })], [7]);
});

test('a GAP aborts the session and refuses to splice', async () => {
  // A spliced ciphertext decrypts to garbage from the gap on, so this must
  // fail rather than return plausible-looking bytes.
  const r = rig((_req, push) => {
    push(chunk(SID, 'a.vdlg', 0n, new Uint8Array([1, 2])));
    push(chunk(SID, 'a.vdlg', 99n, new Uint8Array([3])));   // not 2
  });
  await assert.rejects(() => readFile(r.io, 'a.vdlg', { sessionId: SID }),
    (e: DiagError) => e.code === 'gap');
  assert.equal(decodeReq(r.sent.at(-1)!).body.case, 'abort', 'the session must be aborted');
});

test("a chunk for another FILE in our session is a gap, not data", async () => {
  const r = rig((_req, push) => push(chunk(SID, 'other.vdlg', 0n, new Uint8Array([9]))));
  await assert.rejects(() => readFile(r.io, 'a.vdlg', { sessionId: SID }),
    (e: DiagError) => e.code === 'gap');
});

test('a read honours its offset: the first expected byte is the offset', async () => {
  const r = rig((_req, push) => push(chunk(SID, 'a.vdlg', 100n, new Uint8Array([1]), true)));
  assert.deepEqual([...await readFile(r.io, 'a.vdlg', { sessionId: SID, offset: 100n })], [1]);
});

test('onProgress reports CUMULATIVE bytes', async () => {
  const seen: number[] = [];
  const r = rig((_req, push) => {
    push(chunk(SID, 'a.vdlg', 0n, new Uint8Array(3)));
    push(chunk(SID, 'a.vdlg', 3n, new Uint8Array(2), true));
  });
  await readFile(r.io, 'a.vdlg', { sessionId: SID, onProgress: (n) => seen.push(n) });
  assert.deepEqual(seen, [3, 5]);
});

test('consecutive calls take their OWN session, so a stale DONE is ignored', async () => {
  // The hardware bug this exists for: a read ends at its last chunk while the
  // device is still sending a trailing DONE. Under a shared id that DONE ends
  // the NEXT read empty — file two of a pull came back 0 B. Every other case
  // here pins sessionId explicitly, so without this one the default path
  // (`o.sessionId ?? nextSessionId()`) is never exercised at all.
  const sessions: bigint[] = [];
  let trailing: Reply | undefined;
  const io: DiagIo = {
    send(p) {
      const sid = decodeReq(p).sessionId;
      sessions.push(sid);
      // The PREVIOUS read's trailing DONE is still in flight.
      queued = [
        ...(trailing ? [trailing] : []),
        chunk(sid, 'a.vdlg', 0n, new Uint8Array([sessions.length]), true),
      ];
      trailing = status(sid, DiagStatus_State.DONE);
      return true;
    },
    async recv() { return queued.shift() ?? null; },
    clock: () => 0,
  };
  let queued: Reply[] = [];
  const first = await readFile(io, 'a.vdlg');
  const second = await readFile(io, 'a.vdlg');
  assert.notEqual(sessions[0], sessions[1], 'each call must take its own session');
  assert.deepEqual([...first], [1]);
  assert.deepEqual([...second], [2], 'the previous read\'s DONE must not end this one empty');
});

test('a FAILED status during a READ raises the device code', async () => {
  const r = rig((_req, push) => push(status(SID, DiagStatus_State.FAILED, 'no_such_file')));
  await assert.rejects(() => readFile(r.io, 'missing.vdlg', { sessionId: SID }),
    (e: DiagError) => e.code === 'no_such_file');
});

test('a read carries its length to the device', async () => {
  // `length` is what bounds a partial read; nothing else pins that it is sent.
  const r = rig((_req, push) => push(chunk(SID, 'a.vdlg', 0n, new Uint8Array([1]), true)));
  await readFile(r.io, 'a.vdlg', { sessionId: SID, length: 512 });
  const req = decodeReq(r.sent[0]!);
  assert.equal(req.body.case === 'read' ? req.body.value.len : -1, 512);
});

test('a null BEFORE the deadline is retried, not fatal', async () => {
  // A spurious wakeup (or a recv that returns early) must not abort a healthy
  // transfer. Every other silence case here hits the deadline on the first null.
  let calls = 0;
  const io: DiagIo = {
    send: () => true,
    async recv() {
      calls += 1;
      if (calls > 8) throw new Error('the driver never stopped retrying');
      // Three early nulls that consume NO time, then the answer.
      return calls <= 3 ? null : chunk(SID, 'a.vdlg', 0n, new Uint8Array([9]), true);
    },
    clock: () => 0,
  };
  assert.deepEqual([...await readFile(io, 'a.vdlg', { sessionId: SID, timeoutS: 5 })], [9]);
});

test('the listing reports size and bytesValid distinctly', async () => {
  // The app sizes its pull off bytesValid; swapping the two is invisible
  // unless they differ.
  const r = rig((_req, push) => push(listing(SID, [
    { name: 'a.vdlg', size: 4096n, bytesValid: 768n, tier: 1 },
  ])));
  const [f] = await listFiles(r.io, { sessionId: SID });
  assert.equal(f!.size, 4096n);
  assert.equal(f!.bytesValid, 768n);
});

test('an abort carries the same session and target as the read it ends', async () => {
  // On a shared bus leg an unaddressed abort is a no-op, and the device keeps
  // streaming into the next request.
  const r = rig((_req, push) => {
    push(chunk(SID, 'a.vdlg', 0n, new Uint8Array([1, 2])));
    push(chunk(SID, 'a.vdlg', 99n, new Uint8Array([3])));
  });
  await assert.rejects(() => readFile(r.io, 'a.vdlg', { sessionId: SID, targetDevice: 'GILABS-A' }));
  const abort = decodeReq(r.sent.at(-1)!);
  assert.equal(abort.body.case, 'abort');
  assert.equal(abort.sessionId, SID);
  assert.equal(abort.targetDevice, 'GILABS-A');
});

// -- the DiagIo contract -------------------------------------------------- //

test('a recv that REUSES its buffer still yields correct bytes', async () => {
  // protobuf-es decodes zero-copy, so a retained chunk is a view into whatever
  // buffer recv returned. A serial or socket reader naturally reuses one, and
  // retaining views would then assemble whatever that buffer held at the END of
  // the transfer — for a .vdlg, ciphertext garbage nothing can detect.
  const frames = [
    chunk(SID, 'a.vdlg', 0n, new Uint8Array([1, 2, 3])),
    chunk(SID, 'a.vdlg', 3n, new Uint8Array([4, 5]), true),
  ];
  const scratch = new Uint8Array(256);
  let i = 0;
  const io: DiagIo = {
    send: () => true,
    async recv() {
      const f = frames[i];
      i += 1;
      if (f === undefined) return null;
      scratch.fill(0xee);                 // the previous frame's bytes are GONE
      scratch.set(f, 0);
      return scratch.subarray(0, f.length);
    },
    clock: () => 0,
  };
  assert.deepEqual([...await readFile(io, 'a.vdlg', { sessionId: SID })], [1, 2, 3, 4, 5]);
});

test('a refused send is reported as a dead link, not waited out', async () => {
  // Otherwise the caller pays the full stall window and is told `timeout`,
  // which names the wrong cause. wire/ota acts on the same signal.
  let waited = 0;
  let calls = 0;
  const io: DiagIo = {
    send: () => false,
    async recv(t) {
      waited += t;
      calls += 1;
      if (calls > 3) throw new Error('a refused send was waited out instead of reported');
      return null;
    },
    clock: () => 0,
  };
  await assert.rejects(() => listFiles(io, { sessionId: SID, timeoutS: 30 }),
    (e: DiagError) => e.code === 'link');
  assert.equal(waited, 0, 'it must not wait on a link that refused the request');
});

// -- the shared channel --------------------------------------------------- //

test("another session's replies are ignored", async () => {
  const r = rig((_req, push) => {
    push(chunk(0x999n, 'a.vdlg', 0n, new Uint8Array([42])));   // not ours
    push(chunk(SID, 'a.vdlg', 0n, new Uint8Array([1]), true));
  });
  assert.deepEqual([...await readFile(r.io, 'a.vdlg', { sessionId: SID })], [1]);
});

test('an undecodable frame is FATAL, not skipped', async () => {
  // The caller filters to the device's diag channel before calling recv, so a
  // frame that will not decode is schema drift. Skipping it would wait out a
  // real failure until the stall timer, and report the wrong cause. Python
  // agrees by omission: ParseFromString raises and nothing catches it.
  const r = rig((_req, push) => push(new Uint8Array([0xff, 0xff, 0xff, 0xff])));
  await assert.rejects(() => readFile(r.io, 'a.vdlg', { sessionId: SID }),
    (e: DiagError) => e.code === 'decode');
});

test('silence for the stall window is a timeout', async () => {
  const r = rig(() => { /* the device says nothing at all */ });
  await assert.rejects(() => readFile(r.io, 'a.vdlg', { sessionId: SID, timeoutS: 3 }),
    (e: DiagError) => e.code === 'timeout');
  assert.ok(r.at() >= 3, 'the full stall window must elapse before giving up');
});

test('the stall window starts at the SEND, not after an awaited send', async () => {
  // `send` may yield. A caller whose clock steps rather than ticks (a fake in a
  // consumer's suite, say) would then have the window begin after time had
  // already moved, and the read could never time out — an infinite loop, not a
  // late error. Caught when visio-companion's diag suite hung on this.
  let now = 0;
  let calls = 0;
  const io: DiagIo = {
    async send() { now = 1000; return true; },   // time jumps during the send
    async recv() {
      calls += 1;
      if (calls > 3) throw new Error('the stall window never expired');
      return null;
    },
    clock: () => now,
  };
  await assert.rejects(() => listFiles(io, { sessionId: SID, timeoutS: 0.1 }),
    (e: DiagError) => e.code === 'timeout');
});

test('a slow but live read does NOT trip the stall timer', async () => {
  // The deadline is only ever CONSULTED when recv returns null, so a device
  // that keeps the queue full can never exercise the reset — an earlier
  // version of this test queued six replies and passed with the reset deleted.
  // A real slow device is silent BETWEEN chunks: here each gap costs 0.9 of a
  // window, so the read spans 5+ windows in total. Only a deadline that resets
  // on every reply survives it; one computed once expires on the second gap.
  const frames = [
    chunk(SID, 'a.vdlg', 0n, new Uint8Array([1])),
    chunk(SID, 'a.vdlg', 1n, new Uint8Array([2])),
    chunk(SID, 'a.vdlg', 2n, new Uint8Array([3])),
    chunk(SID, 'a.vdlg', 3n, new Uint8Array([4]), true),
  ];
  let now = 0;
  let i = 0;
  let silent = true;
  const io: DiagIo = {
    send: () => true,
    async recv(timeoutS) {
      if (silent) {                 // a gap, just short of the window
        silent = false;
        now += Math.min(timeoutS, 0.9);
        return null;
      }
      silent = true;
      const f = frames[i];
      i += 1;
      if (f === undefined) throw new Error('the driver read past the last chunk');
      return f;
    },
    clock: () => now,
  };
  const out = await readFile(io, 'a.vdlg', { sessionId: SID, timeoutS: 1 });
  assert.deepEqual([...out], [1, 2, 3, 4]);
  assert.ok(now > 1, `the read must outlast one stall window (spanned ${now})`);
});
