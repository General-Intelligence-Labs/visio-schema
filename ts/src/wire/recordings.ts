/**
 * Pull recorded sessions off a device: list, open, receive, delete. Owns no bus
 * connection.
 *
 * The TypeScript twin of `visio_schema/wire/recordings.py`. Control rides the
 * existing Command / CommandResult pair (ListRecordings, OpenRecordingFile,
 * DeleteRecording). The file bytes do NOT ride the bus: an open names a TCP
 * port, and the host connects to it on the device address it sent the open to,
 * reads until the device closes, and never writes. Any direct IP link serves a
 * pull (USB-NCM or Wi-Fi). Exactly `length` bytes means the range arrived
 * whole; fewer means open again at the bytes received. The contract is
 * docs/protocol/recordings_pull.md.
 *
 * Both seams are injected. `run` sends one Command and resolves with its
 * CommandResult (the caller stamps commandId and matches the reply). `receive`
 * reads one opened range into wherever the bytes belong — a React Native app
 * does that natively, straight to a file, and Node does it with `node:net` —
 * so this module imports neither.
 */
import { create, fromBinary, toBinary } from '@bufbuild/protobuf';

import {
  CommandSchema,
  DeleteRecordingSchema,
  ListRecordingsSchema,
  OpenRecordingFileSchema,
  type Command,
} from '../gen/visio_schema/v1/control/command_pb.js';
import {
  CommandResultSchema,
  type CommandResult,
  type RecordingEntry,
  type RecordingFileOpen,
  type RecordingsList,
} from '../gen/visio_schema/v1/control/command_result_pb.js';

/** Where current firmware's file sender listens (the open reports the port). */
export const DEFAULT_PORT = 50002;
export const COMMAND_TIMEOUT_S = 8.0;
/** No byte for this long on the data socket: the transfer is dead; resume. */
export const STALL_TIMEOUT_S = 10.0;
export const MAX_OPENS_WITHOUT_PROGRESS = 5;
/**
 * What a host drops on its own bus link while it pulls (SetStreamPolicy rules,
 * recordings_pull.md §7): video, IMU samples, audio. Restore on every exit.
 */
export const PULL_QUIESCE_RULES = ['**/camera/*', '**/imu/*/raw', '**/imu/*/quat', '**/audio/*'] as const;
/**
 * Names and the cursor are capped at 63 UTF-8 bytes (nanopb max_size:64). An
 * oversized string fails the device's whole decode and it answers nothing, so
 * the cap is enforced before anything is sent.
 */
export const MAX_NAME_BYTES = 63;

/** The device refused (`code` = CommandResult.errorCode), or a pull could not finish. */
export class RecordingsError extends Error {
  readonly code: string;
  constructor(code: string, message = '') {
    super(message ? `${code}: ${message}` : code);
    this.name = 'RecordingsError';
    this.code = code;
  }
}

/** Send one Command (stamp commandId first) and resolve with its CommandResult. */
export type RunCommand = (command: Command, timeoutS: number) => Promise<CommandResult>;

export interface Received {
  /** Bytes of the range that reached their destination. */
  readonly received: bigint;
  /** Why it stopped short, if it did — for the error message only. */
  readonly error?: string;
}

/**
 * Read one opened range: connect to `open.port` on the device address the open
 * was sent to, store bytes from `open.offset` in order, stop at `open.length`
 * or when the device closes. A short read is a value, not a throw. Throw only
 * for a failure retrying cannot fix (a full disk), and that ends the pull.
 * The bus link must keep being read meanwhile (recordings_pull.md §4).
 */
export type Receive = (open: RecordingFileOpen, stallTimeoutS: number) => Promise<Received>;

const utf8Bytes = (s: string): number => new TextEncoder().encode(s).length;

function checkName(what: string, name: string, required = true): void {
  if (!name) {
    if (required) throw new RangeError(`${what} is empty`);
    return;
  }
  const n = utf8Bytes(name);
  if (n > MAX_NAME_BYTES) throw new RangeError(`${what} is ${n} bytes; the limit is ${MAX_NAME_BYTES}`);
  if (name.includes('/') || name.includes('\0') || name.startsWith('.')) {
    throw new RangeError(`${what} ${JSON.stringify(name)} is not a plain name`);
  }
}

export interface Addressed {
  targetDevice?: string;
}

/** A ListRecordings. No cursor and no session = the original newest-`limit` call. */
export function listRecordingsCommand(
  o: Addressed & { limit?: number; cursor?: string; sessionName?: string } = {},
): Command {
  const cursor = o.cursor ?? '';
  if (utf8Bytes(cursor) > MAX_NAME_BYTES) throw new RangeError(`cursor is over ${MAX_NAME_BYTES} bytes`);
  checkName('sessionName', o.sessionName ?? '', false);
  return create(CommandSchema, {
    targetDevice: o.targetDevice ?? '',
    body: {
      case: 'listRecordings',
      value: create(ListRecordingsSchema, {
        limit: o.limit ?? 0,
        cursor,
        sessionName: o.sessionName ?? '',
      }),
    },
  });
}

export interface OpenOptions extends Addressed {
  offset?: bigint;
  /** Both 0n = unchecked. */
  expectSize?: bigint;
  expectMtimeNs?: bigint;
}

export function openRecordingFileCommand(sessionName: string, fileName: string, o: OpenOptions = {}): Command {
  checkName('sessionName', sessionName);
  checkName('fileName', fileName);
  return create(CommandSchema, {
    targetDevice: o.targetDevice ?? '',
    body: {
      case: 'openRecordingFile',
      value: create(OpenRecordingFileSchema, {
        sessionName,
        fileName,
        offset: o.offset ?? 0n,
        expectSize: o.expectSize ?? 0n,
        expectMtimeNs: o.expectMtimeNs ?? 0n,
      }),
    },
  });
}

/** A DeleteRecording. The device refuses a broadcast delete, so a target is required. */
export function deleteRecordingCommand(sessionName: string, targetDevice: string): Command {
  checkName('sessionName', sessionName);
  if (!targetDevice) throw new RangeError('deleteRecording needs targetDevice: a broadcast delete is refused');
  return create(CommandSchema, {
    targetDevice,
    body: { case: 'deleteRecording', value: create(DeleteRecordingSchema, { sessionName }) },
  });
}

export const encodeCommand = (c: Command): Uint8Array => toBinary(CommandSchema, c);
export const decodeResult = (bytes: Uint8Array): CommandResult => fromBinary(CommandResultSchema, bytes);

async function runOk(run: RunCommand, cmd: Command, timeoutS: number): Promise<CommandResult> {
  const result = await run(cmd, timeoutS);
  if (!result.ok) throw new RecordingsError(result.errorCode || 'failed', result.errorMessage);
  return result;
}

function listing(result: CommandResult): RecordingsList {
  if (result.payload.case !== 'recordings') {
    throw new RecordingsError('protocol', `ListRecordings answered payload ${String(result.payload.case)}`);
  }
  return result.payload.value;
}

export async function listRecordings(
  run: RunCommand,
  o: Addressed & { limit?: number; cursor?: string; timeoutS?: number } = {},
): Promise<RecordingsList> {
  const cmd = listRecordingsCommand({ limit: o.limit, cursor: o.cursor, targetDevice: o.targetDevice });
  return listing(await runOk(run, cmd, o.timeoutS ?? COMMAND_TIMEOUT_S));
}

/** Every session, following cursors to the last page. */
export async function listAllRecordings(
  run: RunCommand,
  o: Addressed & { pageLimit?: number; timeoutS?: number } = {},
): Promise<RecordingEntry[]> {
  const out: RecordingEntry[] = [];
  const seen = new Set<string>();
  let cursor = '';
  for (;;) {
    const page = await listRecordings(run, { limit: o.pageLimit, cursor, targetDevice: o.targetDevice, timeoutS: o.timeoutS });
    out.push(...page.recordings);
    if (!page.nextCursor) return out;
    if (seen.has(page.nextCursor)) throw new RecordingsError('protocol', `cursor ${page.nextCursor} did not advance`);
    seen.add(page.nextCursor);
    cursor = page.nextCursor;
  }
}

/** One session with its files (`RecordingEntry.files`). */
export async function listSessionFiles(
  run: RunCommand,
  sessionName: string,
  o: Addressed & { timeoutS?: number } = {},
): Promise<RecordingEntry> {
  const cmd = listRecordingsCommand({ sessionName, targetDevice: o.targetDevice });
  const page = listing(await runOk(run, cmd, o.timeoutS ?? COMMAND_TIMEOUT_S));
  const only = page.recordings.length === 1 ? page.recordings[0] : undefined;
  if (!only || only.name !== sessionName) {
    throw new RecordingsError('protocol', `asked for ${sessionName}, got ${page.recordings.map((e) => e.name).join(',')}`);
  }
  return only;
}

/** Open one file from `offset`; the answer says where and how much to read. */
export async function openRecordingFile(
  run: RunCommand,
  sessionName: string,
  fileName: string,
  o: OpenOptions & { timeoutS?: number } = {},
): Promise<RecordingFileOpen> {
  const offset = o.offset ?? 0n;
  const result = await runOk(run, openRecordingFileCommand(sessionName, fileName, o), o.timeoutS ?? COMMAND_TIMEOUT_S);
  if (result.payload.case !== 'fileOpen') {
    throw new RecordingsError('protocol', `OpenRecordingFile answered payload ${String(result.payload.case)}`);
  }
  const opened = result.payload.value;
  if (opened.offset !== offset || opened.offset + opened.length !== opened.fileSize) {
    throw new RecordingsError(
      'protocol',
      `open for offset ${offset} answered offset ${opened.offset}, length ${opened.length}, size ${opened.fileSize}`,
    );
  }
  return opened;
}

export interface PullIo {
  run: RunCommand;
  receive: Receive;
  onProgress?: (offset: bigint, size: bigint) => void;
}

export interface PullOptions extends OpenOptions {
  stallTimeoutS?: number;
  commandTimeoutS?: number;
  maxOpensWithoutProgress?: number;
}

/**
 * Pull `fileName` from `offset` to its end. On a short read, open again at the
 * bytes received with the size and mtime of the first open pinned, so a file
 * that changes between attempts fails "changed" instead of being spliced.
 * Resolves with the last open (its fileSize and mtimeNs identify the file).
 */
export async function pullFile(
  io: PullIo,
  sessionName: string,
  fileName: string,
  o: PullOptions = {},
): Promise<RecordingFileOpen> {
  let offset = o.offset ?? 0n;
  let expectSize = o.expectSize ?? 0n;
  let expectMtimeNs = o.expectMtimeNs ?? 0n;
  const maxIdle = o.maxOpensWithoutProgress ?? MAX_OPENS_WITHOUT_PROGRESS;
  let idleOpens = 0;
  for (;;) {
    const opened = await openRecordingFile(io.run, sessionName, fileName, {
      offset,
      expectSize,
      expectMtimeNs,
      targetDevice: o.targetDevice,
      timeoutS: o.commandTimeoutS,
    });
    expectSize = opened.fileSize;
    expectMtimeNs = opened.mtimeNs;
    if (opened.length === 0n) return opened;
    const got = await io.receive(opened, o.stallTimeoutS ?? STALL_TIMEOUT_S);
    offset += got.received;
    io.onProgress?.(offset, opened.fileSize);
    if (offset === opened.fileSize) return opened;
    if (got.received > 0n) {
      idleOpens = 0;
    } else {
      idleOpens += 1;
      if (idleOpens >= maxIdle) {
        throw new RecordingsError('no_progress', `${idleOpens} opens delivered no bytes at offset ${offset} (last: ${got.error ?? 'closed'})`);
      }
    }
  }
}

/** Delete one whole session. Refusals throw RecordingsError with the device's code. */
export async function deleteRecording(
  run: RunCommand,
  sessionName: string,
  targetDevice: string,
  o: { timeoutS?: number } = {},
): Promise<void> {
  await runOk(run, deleteRecordingCommand(sessionName, targetDevice), o.timeoutS ?? COMMAND_TIMEOUT_S);
}
