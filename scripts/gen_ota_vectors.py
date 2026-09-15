#!/usr/bin/env python3
"""Generate tests/golden/ota_vectors.txt — the cross-language OTA transcript.

The four existing golden files pin a CODEC: one input, one expected byte string.
An OTA driver is a state machine, so its vector is a TRANSCRIPT — the ordered
messages it must emit against a scripted device, and the outcome it must reach.

Format: the same `key=lowercase_hexbytes` grammar the other four use, so their
~10-line loaders (Python, in-repo C++, cross-repo C++, Go) read this unchanged.

  <case>.fw / .board / .target   the call's string arguments
  <case>.params                  u64 total | u32 chunk | u32 window |
                                 u32 chunk_cap | u64 session |
                                 u8 rsv | u8 negotiate | u8 commit | u8 rsv
  <case>.<NN>.send               the NNth OtaMessage the driver MUST emit
  <case>.<NN>.reply.<MM>         OtaStatus payloads that become available
                                 AFTER that send
  <case>.out                     u8 ok | u8 reason | u64 acked | u64 total |
                                 u32 resumes

WHAT IT PINS, AND WHAT IT DOES NOT. Sends and the outcome, never the recv call
pattern: the Python drains with recv(0.0) and the TypeScript driver is async and
cannot, so pinning the call pattern would freeze an implementation detail and
make one of the three unwritable. Conformance is not equivalence either — it
cannot reach the host outbox depth or the CDC-ACM FIFO. Keep this set tight to
the rules test_wire_ota.py names.

REGENERATION. This writes from the Python reference, so it proves cross-language
agreement, NOT that the behaviour is right — that is test_wire_ota.py's job, and
for the shipped ego bytes it is visio-embedded's tests/golden/ota_ego_*.txt,
which pins the device's own bytes and is not this generator's to write.
"""
import struct
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "python"))

from visio_schema.v1.service.ota import ota_pb2  # noqa: E402
from visio_schema.wire import ota, package  # noqa: E402

OS = ota_pb2.OtaStatus
GOLDEN = _HERE.parent / "tests" / "golden"
OUT = GOLDEN / "ota_vectors.txt"
PKG_OUT = GOLDEN / "package_vectors.txt"
PKG_KEY = bytes(range(0xA0, 0xC0))


def image(n):
    """A formula, not a 20 KB hex blob — and non-trivial, so an offset bug that
    bytes(n) would hide shows up as a wrong chunk payload."""
    return bytes(((i * 31 + 7) & 0xFF) for i in range(n))


class Scripted:
    """A device whose replies are decided by `policy(dev, msg)`, recording the
    exact frames the driver emitted."""

    def __init__(self, policy, clock):
        self.policy, self.clock = policy, clock
        self.steps = []          # [(sent_bytes, [reply_bytes, ...]), ...]
        self.outbox = []
        self.acked = 0

    def send(self, payload):
        self.steps.append((payload, []))
        self.policy(self, ota_pb2.OtaMessage.FromString(payload))

    def say(self, state, *, acked=None, error_code="", max_chunk=0):
        s = OS(state=state, bytes_received=self.acked if acked is None else acked,
               error_code=error_code, max_chunk_bytes=max_chunk)
        raw = s.SerializeToString()
        self.outbox.append(raw)
        self.steps[-1][1].append(raw)

    def recv(self, timeout):
        if self.outbox:
            return self.outbox.pop(0)
        if timeout:
            self.clock.sleep(timeout)
        return None


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


def ack_contiguous(max_chunk=0):
    def policy(dev, m):
        if m.HasField("query"):
            if max_chunk:
                dev.say(OS.IDLE, acked=0, max_chunk=max_chunk)
        elif m.HasField("chunk"):
            if m.chunk.offset == dev.acked:
                dev.acked = m.chunk.offset + len(m.chunk.data)
            dev.say(OS.RECEIVING)
        elif m.HasField("commit"):
            dev.say(OS.STAGED)
    return policy


def refuse(code):
    def policy(dev, m):
        if m.HasField("begin"):
            dev.say(OS.FAILED, error_code=code)
    return policy


def instant_revert(dev, m):
    """fw_version == the other slot's version: STAGED with no transfer."""
    if m.HasField("begin"):
        dev.say(OS.STAGED, acked=0)


def gap_once():
    seen = []

    def policy(dev, m):
        if m.HasField("chunk"):
            if not seen and m.chunk.offset > 0:
                seen.append(1)
            if m.chunk.offset == dev.acked:
                dev.acked = m.chunk.offset + len(m.chunk.data)
            elif not seen:
                pass
            dev.say(OS.RECEIVING)
        elif m.HasField("commit"):
            dev.say(OS.STAGED)
    return policy


CASES = {
    # The shape every single-board push takes, with a board that advertises.
    "happy_advert": dict(policy=ack_contiguous(16 * 1024), total=4096,
                         chunk=1024, window=4096, fw="1.1.5",
                         board="audio_ego_v3"),
    # Firmware predating max_chunk_bytes: silence keeps the caller's chunk.
    "happy_legacy": dict(policy=ack_contiguous(0), total=4096, chunk=1024,
                         window=4096, fw="1.1.5", board="audio_ego_v3"),
    # A/B instant revert — STAGED before a single chunk.
    "instant_revert": dict(policy=instant_revert, total=4096, chunk=1024,
                           window=4096, fw="1.1.3", board="audio_ego_v3"),
    # Refused before a byte moves.
    "wrong_board": dict(policy=refuse("wrong_board"), total=4096, chunk=1024,
                        window=4096, fw="1.1.5", board="ego_v2"),
    "busy_recording": dict(policy=refuse("busy_recording"), total=4096,
                           chunk=1024, window=4096, fw="1.1.5", board=""),
    # Addressed THROUGH a hub at a named unit -- how a head drives one limb.
    # No hold: a commit applies, on a limb exactly as on a single board.
    "addressed_leaf": dict(policy=ack_contiguous(0), total=2048, chunk=1024,
                           window=2048, fw="1.2.0", board="compact_umi",
                           target="GILABS-AAAAAAAA"),
    # Upload proven, nothing flashed.
    "no_flash": dict(policy=ack_contiguous(0), total=2048, chunk=1024,
                     window=2048, fw="1.1.5", board="audio_ego_v3",
                     commit=False),
    # A device that asks for more than nanopb can decode. The advert WINS over
    # the caller's want, but only up to MAX_CHUNK_BYTES -- above it the device
    # answers every frame with a decode error and the transfer never advances,
    # so a sender clamps rather than trusting what it was told. This case exists
    # because the ceiling was the ONE constant the three implementations
    # disagreed on (60 KiB against a bare 65535) while every scripted device
    # advertised a value they happened to agree about.
    "advert_above_ceiling": dict(policy=ack_contiguous(256 * 1024), total=4096,
                                 chunk=1024, window=131072, fw="1.1.5",
                                 board="audio_ego_v3"),
    # And one asking for less than the device's own ack interval. The floor
    # clamps it UP: the device paces its RECEIVING acks off chunk_bytes and
    # those acks are the sender's window credit, so a tiny chunk starves the
    # window instead of making the transfer safer.
    "advert_below_floor": dict(policy=ack_contiguous(512), total=32768,
                               chunk=1024, window=65536, fw="1.1.5",
                               board="audio_ego_v3"),
}


def run(name, spec):
    clock = Clock()
    dev = Scripted(spec["policy"], clock)
    session = 0xCA11
    out = ota.relay(
        dev.send, dev.recv, image(spec["total"]),
        fw_version=spec["fw"], board=spec.get("board", ""),
        target_device=spec.get("target", ""), session_id=session,
        commit=spec.get("commit", True),
        window=spec["window"], chunk=spec["chunk"],
        negotiate=spec.get("negotiate", True), clock=clock)
    return dev, out, session


def main():
    lines = [
        "# ota_vectors.txt - the OTA driver transcript, shared by every language.",
        "#",
        "# Generated by scripts/gen_ota_vectors.py from the Python reference in",
        "# visio_schema/wire/ota.py. It pins the three implementations to each",
        "# OTHER; whether the behaviour is RIGHT is python/tests/test_wire_ota.py,",
        "# and for the shipped ego bytes it is visio-embedded's",
        "# tests/golden/ota_ego_*.txt, which pins the device's own bytes",
        "# and is not this generator's to write.",
        "#",
        "# Grammar (see the generator's docstring):",
        "#   <case>.fw|.board|.target   the call's string arguments",
        "#   <case>.params              u64 total|u32 chunk|u32 window|"
        "u32 cap|u64 session|u8 rsv|u8 negotiate|u8 commit|u8 rsv",
        "#   <case>.<NN>.send           the NNth OtaMessage the driver MUST emit",
        "#   <case>.<NN>.reply.<MM>     statuses available AFTER that send",
        "#   <case>.out                 u8 ok|u8 reason|u64 acked|u64 total|"
        "u32 resumes",
        "#",
        "# The image is a formula: image[i] = (i * 31 + 7) & 0xFF.",
    ]
    for name in sorted(CASES):
        spec = CASES[name]
        dev, out, session = run(name, spec)
        lines.append("")
        lines.append(f"{name}.fw={spec['fw'].encode().hex()}")
        lines.append(f"{name}.board={spec.get('board', '').encode().hex()}")
        lines.append(f"{name}.target={spec.get('target', '').encode().hex()}")
        lines.append(f"{name}.params=" + struct.pack(
            ">QIIIQBBBB", spec["total"], spec["chunk"], spec["window"],
            spec.get("cap", 0), session, 0,
            int(spec.get("negotiate", True)), int(spec.get("commit", True)),
            0).hex())
        for i, (sent, replies) in enumerate(dev.steps):
            lines.append(f"{name}.{i:02d}.send={sent.hex()}")
            for j, r in enumerate(replies):
                lines.append(f"{name}.{i:02d}.reply.{j:02d}={r.hex()}")
        lines.append(f"{name}.out=" + struct.pack(
            ">BBQQI", int(out.ok), int(out.reason), out.acked, out.total,
            out.resumes).hex())
    OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT} ({len(lines)} lines, {len(CASES)} cases)")
    write_package_vectors()


def write_package_vectors():
    """One real package, so the C++ reader is checked against the Python writer.

    A format with a writer in one language and a reader in another is exactly
    where a field silently stops being read, so the fixture is the whole
    artifact rather than a description of one.
    """
    images = [
        {"hwrev": "compact_umi", "equipment": "gripper", "soc": "rv1106",
         "blob": image(600)},
        {"hwrev": "ego_pro_head", "equipment": "ego", "soc": "rv1126b",
         "blob": image(1500)},
    ]
    pkg = package.build(images, product="ego_pro", version="1.3.0",
                        key=PKG_KEY, self_hwrev="ego_pro_head")
    idx = package.read_index(pkg, PKG_KEY)

    lines = [
        "# package_vectors.txt - one real OTA package, built by",
        "# python/visio_schema/wire/package.py and parsed by BOTH",
        "# python/tests/test_wire_package.py and cpp/tests/test_package.cc.",
        "#",
        "# A format whose writer and reader are in different languages is",
        "# exactly where a field silently stops being read, so the fixture is",
        "# the whole artifact rather than a description of one.",
        "#",
        "# Image blobs are the same formula the OTA vectors use:",
        "#   image[i] = (i * 31 + 7) & 0xFF",
        "",
        f"key={PKG_KEY.hex()}",
        f"pkg={pkg.hex()}",
        f"product={idx['product'].encode().hex()}",
        f"version={idx['version'].encode().hex()}",
    ]
    for i, b in enumerate(idx["boards"]):
        lines.append(f"board.{i:02d}.hwrev={b['hwrev'].encode().hex()}")
        lines.append(f"board.{i:02d}.file={b['file'].encode().hex()}")
        lines.append(f"board.{i:02d}.bytes={struct.pack('>Q', b['bytes']).hex()}")
        lines.append(f"board.{i:02d}.sha256={b['sha256']}")
    PKG_OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {PKG_OUT} ({len(idx['boards'])} boards, {len(pkg)} pkg bytes)")


if __name__ == "__main__":
    main()
