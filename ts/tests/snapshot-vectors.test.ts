import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fromBinary, toBinary } from '@bufbuild/protobuf';
import { CommandSchema } from '../src/gen/visio_schema/v1/control/command_pb.js';
import { CommandResultSchema } from '../src/gen/visio_schema/v1/control/command_result_pb.js';
import { SnapshotChunkSchema } from '../src/gen/visio_schema/v1/service/snapshot/snapshot_pb.js';
import { loadGolden } from './golden.js';

const vectors = loadGolden('snapshot_vectors.txt');
test('one capture, metadata and finite stream round-trip the shared corpus', () => {
  assert.equal(vectors.size, 8);
  for (const [name, raw] of vectors) {
    const encoded = name.endsWith('.cmd')
      ? toBinary(CommandSchema, fromBinary(CommandSchema, raw))
      : name.endsWith('.chunk')
        ? toBinary(SnapshotChunkSchema, fromBinary(SnapshotChunkSchema, raw))
        : toBinary(CommandResultSchema, fromBinary(CommandResultSchema, raw));
    assert.deepEqual(encoded, raw, name);
  }
  const capture = fromBinary(CommandSchema, vectors.get('capture.cmd')!);
  assert.equal(capture.body.case, 'captureSnapshot');
  assert.equal(capture.targetDevice, 'GILABS-TEST');
});
test('metadata and all chunks correlate exactly above the JS integer range', () => {
  const ack = fromBinary(CommandResultSchema, vectors.get('capture.result')!);
  assert.equal(ack.payload.case, 'snapshot');
  if (ack.payload.case !== 'snapshot') return;
  const meta = ack.payload.value;
  assert.equal(meta.capturePtsUs, 12345678901234567n);
  assert.equal(meta.snapshotId, ack.commandId);
  let offset = 0;
  for (const name of ['first.chunk', 'middle.chunk', 'final.chunk']) {
    const chunk = fromBinary(SnapshotChunkSchema, vectors.get(name)!);
    assert.equal(chunk.snapshotId, meta.snapshotId);
    assert.equal(chunk.offset, offset);
    assert.ok(chunk.jpeg.length > 0 && chunk.jpeg.length <= 1024);
    offset += chunk.jpeg.length;
  }
  assert.equal(offset, meta.totalBytes);
  const old = fromBinary(CommandResultSchema, vectors.get('older.result')!);
  assert.equal(old.payload.case, 'state');
  if (old.payload.case === 'state') assert.equal(old.payload.value.snapshotSupported, false);
});
