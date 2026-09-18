/**
 * wire/recordings against a fake device: a command runner plus a real loopback
 * TCP sender that behaves like the firmware's (one connection per open, bytes,
 * close), read by a `node:net` receive — the shape the app implements natively.
 */
import assert from 'node:assert/strict';
import { createServer, connect, type Server, type AddressInfo } from 'node:net';
import { test } from 'node:test';

import { create } from '@bufbuild/protobuf';

import type { Command } from '../src/gen/visio_schema/v1/control/command_pb.js';
import {
  CommandResultSchema,
  RecordingFileOpenSchema,
  RecordingsListSchema,
  type CommandResult,
  type RecordingFileOpen,
} from '../src/gen/visio_schema/v1/control/command_result_pb.js';
import {
  PULL_QUIESCE_RULES,
  RecordingsError,
  listAllRecordings,
  openRecordingFileCommand,
  pullFile,
  type PullIo,
  type Receive,
} from '../src/wire/recordings.js';

const SESSION = 'session_00042-1789017895';
const MTIME = 1789017895000000000n;

class FakeDevice {
  data: Buffer;
  mtime = MTIME;
  cuts: number[] = [];
  refuseSends = false;
  opens: Array<[bigint, bigint, bigint]> = [];
  private pending: { offset: number; length: number } | null = null;
  private server: Server;
  port = 0;

  constructor(size: number) {
    this.data = Buffer.alloc(size);
    for (let i = 0; i < size; i += 1) this.data[i] = (i * 31 + 7) & 0xff;
    this.server = createServer((sock) => {
      const p = this.pending;
      this.pending = null;
      if (!p || this.refuseSends) {
        sock.end();
        return;
      }
      let len = p.length;
      if (this.cuts.length) len = Math.min(len, this.cuts.shift()!);
      sock.on('error', () => {});
      sock.end(this.data.subarray(p.offset, p.offset + len));
    });
  }

  async start(): Promise<void> {
    await new Promise<void>((resolve) => this.server.listen(0, '127.0.0.1', resolve));
    this.port = (this.server.address() as AddressInfo).port;
  }

  close(): void {
    this.server.close();
  }

  run = async (cmd: Command): Promise<CommandResult> => {
    if (cmd.body.case !== 'openRecordingFile') {
      return create(CommandResultSchema, { ok: false, errorCode: 'unsupported' });
    }
    const b = cmd.body.value;
    this.opens.push([b.offset, b.expectSize, b.expectMtimeNs]);
    const size = BigInt(this.data.length);
    if ((b.expectSize || b.expectMtimeNs) && (b.expectSize !== size || b.expectMtimeNs !== this.mtime)) {
      return create(CommandResultSchema, { ok: false, errorCode: 'changed' });
    }
    const length = size - b.offset;
    this.pending = { offset: Number(b.offset), length: Number(length) };
    return create(CommandResultSchema, {
      ok: true,
      payload: {
        case: 'fileOpen',
        value: create(RecordingFileOpenSchema, {
          port: this.port,
          offset: b.offset,
          length,
          fileSize: size,
          mtimeNs: this.mtime,
        }),
      },
    });
  };
}

/** A node:net receive into a Buffer — what the app does natively into a file. */
function netReceive(sink: Buffer[], sinkOffsets: bigint[]): Receive {
  return (open: RecordingFileOpen, stallTimeoutS: number) =>
    new Promise((resolve) => {
      let received = 0n;
      const sock = connect({ host: '127.0.0.1', port: open.port });
      sock.setTimeout(stallTimeoutS * 1000, () => sock.destroy(new Error('stall')));
      sock.on('data', (chunk: Buffer) => {
        sinkOffsets.push(open.offset + received);
        sink.push(Buffer.from(chunk));
        received += BigInt(chunk.length);
      });
      sock.on('close', () => resolve({ received }));
      sock.on('error', (e) => resolve({ received, error: e.message }));
    });
}

async function withDevice(size: number, body: (dev: FakeDevice) => Promise<void>): Promise<void> {
  const dev = new FakeDevice(size);
  await dev.start();
  try {
    await body(dev);
  } finally {
    dev.close();
  }
}

test('pulls a whole file in one open', () =>
  withDevice(3 * 1024 * 1024 + 17, async (dev) => {
    const chunks: Buffer[] = [];
    const offsets: bigint[] = [];
    const io: PullIo = { run: dev.run, receive: netReceive(chunks, offsets) };
    const opened = await pullFile(io, SESSION, 'ego_0000.mcap');
    assert.equal(Buffer.concat(chunks).equals(dev.data), true);
    assert.equal(opened.fileSize, BigInt(dev.data.length));
    assert.equal(dev.opens.length, 1);
  }));

test('an early close resumes at the bytes received with the identity pinned', () =>
  withDevice(2 * 1024 * 1024, async (dev) => {
    dev.cuts = [100_000, 300_000];
    const chunks: Buffer[] = [];
    const io: PullIo = { run: dev.run, receive: netReceive(chunks, []) };
    await pullFile(io, SESSION, 'ego_0000.mcap');
    assert.equal(Buffer.concat(chunks).equals(dev.data), true);
    const size = BigInt(dev.data.length);
    assert.deepEqual(dev.opens, [
      [0n, 0n, 0n],
      [100_000n, size, MTIME],
      [400_000n, size, MTIME],
    ]);
  }));

test('a file that changes between opens fails instead of splicing', () =>
  withDevice(1024 * 1024, async (dev) => {
    dev.cuts = [50_000];
    const chunks: Buffer[] = [];
    const run = async (cmd: Command) => {
      if (dev.opens.length) dev.mtime += 1n;
      return dev.run(cmd);
    };
    await assert.rejects(
      pullFile({ run, receive: netReceive(chunks, []) }, SESSION, 'ego_0000.mcap'),
      (e: unknown) => e instanceof RecordingsError && e.code === 'changed',
    );
    assert.equal(Buffer.concat(chunks).length, 50_000);
  }));

test('opens that deliver nothing give up', () =>
  withDevice(4096, async (dev) => {
    dev.refuseSends = true;
    await assert.rejects(
      pullFile({ run: dev.run, receive: netReceive([], []) }, SESSION, 'ego_0000.mcap', {
        maxOpensWithoutProgress: 3,
      }),
      (e: unknown) => e instanceof RecordingsError && e.code === 'no_progress',
    );
    assert.equal(dev.opens.length, 3);
  }));

test('an already complete file needs no socket', () =>
  withDevice(512, async (dev) => {
    let receives = 0;
    const io: PullIo = {
      run: dev.run,
      receive: async () => {
        receives += 1;
        return { received: 0n };
      },
    };
    const opened = await pullFile(io, SESSION, 'session.json', { offset: 512n });
    assert.equal(opened.length, 0n);
    assert.equal(receives, 0);
  }));

test('a receive that throws ends the pull', () =>
  withDevice(4096, async (dev) => {
    const io: PullIo = {
      run: dev.run,
      receive: async () => {
        throw new Error('ENOSPC');
      },
    };
    await assert.rejects(pullFile(io, SESSION, 'ego_0000.mcap'), /ENOSPC/);
    assert.equal(dev.opens.length, 1);
  }));

test('builders refuse names the device cannot decode', () => {
  for (const bad of ['', '.hidden', 'a/b', 'x'.repeat(64)]) {
    assert.throws(() => openRecordingFileCommand(bad, 'a.mcap'), RangeError);
    assert.throws(() => openRecordingFileCommand(SESSION, bad), RangeError);
  }
  openRecordingFileCommand('s'.repeat(63), 'f'.repeat(63));
});

test('listing follows cursors and refuses one that does not advance', async () => {
  let calls = 0;
  const pages = async (cmd: Command): Promise<CommandResult> => {
    assert.equal(cmd.body.case, 'listRecordings');
    const cursor = cmd.body.case === 'listRecordings' ? cmd.body.value.cursor : '';
    calls += 1;
    return create(CommandResultSchema, {
      ok: true,
      payload: {
        case: 'recordings',
        value: create(RecordingsListSchema, {
          recordings: [{ name: `session_${calls}` }],
          nextCursor: cursor ? '' : 'p2',
        }),
      },
    });
  };
  const all = await listAllRecordings(pages);
  assert.deepEqual(all.map((e) => e.name), ['session_1', 'session_2']);

  const stuck = async (): Promise<CommandResult> =>
    create(CommandResultSchema, {
      ok: true,
      payload: { case: 'recordings', value: create(RecordingsListSchema, { nextCursor: 'same' }) },
    });
  await assert.rejects(listAllRecordings(stuck), (e: unknown) => e instanceof RecordingsError && e.code === 'protocol');
});

test('PULL_QUIESCE_RULES is the recordings_pull.md §7 list', () => {
  // Python PULL_QUIESCE_RULES and C++ kPullQuiesceRules pin the same list.
  assert.deepEqual([...PULL_QUIESCE_RULES], ['**/camera/*', '**/imu/*/raw', '**/imu/*/quat', '**/audio/*']);
});
