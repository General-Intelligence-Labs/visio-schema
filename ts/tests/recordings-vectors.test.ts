/**
 * Replay tests/golden/recordings_wire_vectors.txt through the TypeScript codec.
 *
 * The cross-language half of the recordings pull: the Python reference and the
 * C++ client replay the same file, so the three agree byte for byte on every
 * command a host builds and every result it decodes.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadGolden } from './golden.js';

import {
  decodeResult,
  deleteRecordingCommand,
  encodeCommand,
  listRecordingsCommand,
  openRecordingFileCommand,
} from '../src/wire/recordings.js';


const V = loadGolden('recordings_wire_vectors.txt');
const SESSION = 'session_00042-1789017895';
const CURSOR = 'v1:1789017895000000:session_00042-1789017895';
const MTIME = 1789017895123456789n;

const COMMANDS: Record<string, () => Uint8Array> = {
  list: () => encodeCommand(listRecordingsCommand()),
  list_page: () => encodeCommand(listRecordingsCommand({ limit: 20, cursor: CURSOR })),
  list_session: () => encodeCommand(listRecordingsCommand({ sessionName: SESSION, targetDevice: 'GILABS-A' })),
  open: () => encodeCommand(openRecordingFileCommand(SESSION, 'ego_0003.mcap')),
  open_resume: () =>
    encodeCommand(
      openRecordingFileCommand(SESSION, 'ego_0003.mcap', {
        offset: 100663296n,
        expectSize: 281018368n,
        expectMtimeNs: MTIME,
        targetDevice: 'GILABS-A',
      }),
    ),
  delete: () => encodeCommand(deleteRecordingCommand(SESSION, 'GILABS-A')),
};

const hex = (b: Uint8Array): string => Buffer.from(b).toString('hex');

test('the corpus and this replay agree on the case set', () => {
  const cmds = [...V.keys()].filter((k) => k.endsWith('.cmd')).map((k) => k.slice(0, -4));
  assert.deepEqual(cmds.sort(), Object.keys(COMMANDS).sort());
});

test('every command encodes as the corpus says', () => {
  for (const [name, call] of Object.entries(COMMANDS)) {
    assert.equal(hex(call()), hex(V.get(`${name}.cmd`)!), name);
  }
});

test('an open result decodes', () => {
  const r = decodeResult(V.get('result_open.result')!);
  assert.equal(r.commandId, 7n);
  assert.equal(r.ok, true);
  assert.equal(r.payload.case, 'fileOpen');
  const o = r.payload.value!;
  assert.equal(o.port, 50002);
  assert.equal(o.offset, 100663296n);
  assert.equal(o.length, 180355072n);
  assert.equal(o.fileSize, 281018368n);
  assert.equal(o.mtimeNs, MTIME);
});

test("a session's files decode with their writing flags", () => {
  const r = decodeResult(V.get('result_session_files.result')!);
  assert.equal(r.payload.case, 'recordings');
  const s = r.payload.value!.recordings[0]!;
  assert.equal(s.name, SESSION);
  assert.equal(s.active, true);
  assert.equal(s.fileCount, 3);
  assert.deepEqual(
    s.files.map((f) => [f.name, f.writing, f.complete]),
    [
      ['ego_0000.mcap', false, true],
      ['ego_0001.mcap', true, false],
      ['session.json', true, true],
    ],
  );
});

test('a page decodes with its cursor and policy', () => {
  const r = decodeResult(V.get('result_page.result')!);
  assert.equal(r.payload.case, 'recordings');
  const page = r.payload.value!;
  assert.equal(page.recordings.length, 2);
  assert.equal(page.recordings[1]!.damaged, true);
  assert.equal(page.nextCursor, CURSOR);
  assert.equal(page.readWhileRecording, true);
});

test('a refusal decodes', () => {
  const r = decodeResult(V.get('result_refused.result')!);
  assert.equal(r.ok, false);
  assert.equal(r.errorCode, 'writing');
  assert.equal(r.payload.case, undefined);
});
