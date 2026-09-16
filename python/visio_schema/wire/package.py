"""The multi-board OTA package: one file, one device, the device fans it out.

A rig (a head plus limb leaves) used to be updated by the CALLER -- one delivery
per board, addressed per board, sequenced per board. That put rig topology in
every pusher. A package moves it to the only party that knows the rig: the head
receives ONE file, matches its contents against the limbs actually attached, and
drives them itself.

FORMAT: an uncompressed (stored) ustar archive.

    index.txt           MUST be member 0
    <hwrev>.img         one VENC envelope per board, verbatim
    ...                 the receiving device's OWN image LAST (see below)

The index is `key=value` lines, NOT JSON -- the same grammar tests/golden/*.txt
uses. A device has to parse this, and visio-schema's C++ is deliberately free of
libprotobuf, abseil and everything else; adding a JSON parser to read ~300 bytes
would be the largest dependency in the whole contract library. Twenty lines of
line-splitting is the right size for the job, and `busybox tar xf pkg.img
index.txt && cat index.txt` still reads perfectly on the board.

    v=1
    product=ego_pro
    version=1.3.0
    board=<role>:<hwrev>:<equipment>:<soc>:<file>:<bytes>:<sha256>
    ...                 one per board, in APPLY order (leaves first)
    hmac=<hex>

Tar because it streams by construction -- the reader walks headers and routes
payload ranges without materialising anything -- and because it is inspectable
with tools that already exist at both ends: `tar tvf` on a laptop, `busybox tar
-t` on the board. A dev package you cannot crack open costs debugging time on
every future failure.

INDEX FIRST is a hard rule, not a convention. Tar has no table of contents, so a
reader discovers members as it goes -- and the head must validate the WHOLE
roster before it erases anything. Reading member 0 buys that back; without the
ordering rule tar could not support the invariant at all.

SELF LAST, for the same reason one level down: by the time the receiving device's
own image arrives, every refusal the index can produce has already fired, and
what is left is a write failure. Discovering one BEFORE the inactive slot is
erased is strictly better than after. (Children are spooled to files, which are
free to delete; a slot erase is not.)

THE INDEX IS AUTHENTICATED. Every image is independently key-encrypted and
self-digesting, so an attacker cannot forge one -- but they could RELABEL which
board an image is for, and the receiving device would then hand a genuine head
image to a limb whose own board mark passes. An RV1126B image on an RV1106 is
maskrom-only recovery. The same hole exists today (the pusher supplies the
board), and a container is the cheap moment to close it: one HMAC over ~300
bytes, under the bundle key both ends already hold.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import tarfile

__all__ = [
    "INDEX_NAME",
    "PackageError",
    "build",
    "index_hmac",
    "is_package",
    "read_index",
]

INDEX_NAME = "index.txt"
_INDEX_VERSION = 1

#: ustar puts "ustar" at offset 257 of the first header block. That is the
#: earliest point a reader can tell a package from a bare VENC envelope, and it
#: is why a package must be sniffed on a 512-byte prefix rather than 4 bytes.
_USTAR_OFFSET = 257
_USTAR_MAGIC = b"ustar"
_BLOCK = 512


class PackageError(ValueError):
    """The bytes are not a package we can act on. The message is user-facing."""


def index_hmac(body: str, key: bytes) -> str:
    """HMAC-SHA256 over every index line EXCEPT the hmac line itself.

    Over the exact bytes rather than a re-serialization, so the digest cannot
    depend on how a writer happened to order or space things -- and so a reader
    verifies what it actually read.
    """
    return hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


def _index_body(product: str, version: str, boards: list[dict]) -> str:
    lines = [f"v={_INDEX_VERSION}", f"product={product}", f"version={version}"]
    for b in boards:
        lines.append("board=" + ":".join((
            b["role"], b["hwrev"], b["equipment"], b["soc"], b["file"],
            str(b["bytes"]), b["sha256"])))
    return "\n".join(lines) + "\n"


def build(images: list[dict], *, product: str, version: str, key: bytes,
          self_hwrev: str = "") -> bytes:
    """One package from per-board VENC blobs.

    `images` is the APPLY order -- leaves first -- as
    `{hwrev, equipment, soc, blob, role?}` dicts. `self_hwrev` names the board
    that receives the package, so its image can be placed last.
    """
    if not images:
        raise PackageError("a package needs at least one image")
    seen = set()
    for im in images:
        if im["hwrev"] in seen:
            raise PackageError(f"two images for board {im['hwrev']!r}")
        seen.add(im["hwrev"])

    ordered = [im for im in images if im["hwrev"] != self_hwrev]
    ordered += [im for im in images if im["hwrev"] == self_hwrev]

    boards = [{
        "role": im.get("role", im["hwrev"]),
        "hwrev": im["hwrev"],
        "equipment": im.get("equipment", ""),
        "soc": im.get("soc", ""),
        "fw_version": version,
        "file": f"{im['hwrev']}.img",
        "sha256": hashlib.sha256(im["blob"]).hexdigest(),
        "bytes": len(im["blob"]),
    } for im in ordered]
    body = _index_body(product, version, boards)
    index_bytes = (body + f"hmac={index_hmac(body, key)}\n").encode()

    buf = io.BytesIO()
    # USTAR + no compression: streaming is the whole point, and a reader on the
    # device walks headers rather than seeking.
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        def add(name: str, data: bytes) -> None:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mtime = 0        # reproducible: same inputs, same bytes
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            tf.addfile(ti, io.BytesIO(data))

        add(INDEX_NAME, index_bytes)
        for im in ordered:
            add(f"{im['hwrev']}.img", im["blob"])
    return buf.getvalue()


def is_package(raw: bytes) -> bool:
    """Whether `raw` starts a package. Needs 512 bytes to be sure."""
    return (len(raw) >= _USTAR_OFFSET + len(_USTAR_MAGIC) and
            raw[_USTAR_OFFSET:_USTAR_OFFSET + len(_USTAR_MAGIC)] == _USTAR_MAGIC)


def read_index(raw: bytes, key: bytes | None = None) -> dict:
    """Parse and verify the index out of a package prefix.

    Raises `PackageError` with a reason a human can act on. Verifying the HMAC
    is optional only so a laptop without the bundle key can still LIST a package
    (`--inspect`); a device must always pass the key.
    """
    if not is_package(raw):
        raise PackageError("not a package (no ustar magic)")
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
        first = tf.next()
        if first is None:
            raise PackageError("empty package")
        if first.name != INDEX_NAME:
            raise PackageError(
                f"member 0 is {first.name!r}, not {INDEX_NAME!r} -- a reader "
                f"must be able to validate the whole roster before it writes "
                f"anything, and tar has no table of contents")
        text = tf.extractfile(first).read().decode("utf-8", "replace")
    return parse_index(text, key)


def parse_index(text: str, key: bytes | None = None) -> dict:
    """The `key=value` index as a dict. Split out so a device-side reader and a
    host tool agree on the grammar rather than each inventing one."""
    body, hmac_line = [], ""
    for line in text.splitlines():
        if line.startswith("hmac="):
            hmac_line = line[len("hmac="):]
            continue
        body.append(line)
    body_text = "\n".join(body) + "\n" if body else ""

    index: dict = {"boards": []}
    for line in body:
        key_, _, value = line.partition("=")
        if key_ == "board":
            f = value.split(":")
            if len(f) != 7:
                raise PackageError(f"malformed board line: {line!r}")
            index["boards"].append({
                "role": f[0], "hwrev": f[1], "equipment": f[2], "soc": f[3],
                "file": f[4], "bytes": int(f[5]), "sha256": f[6]})
        elif key_:
            index[key_] = value

    if index.get("v") != str(_INDEX_VERSION):
        raise PackageError(f"index version {index.get('v')!r}, expected "
                           f"{_INDEX_VERSION} -- refusing to guess")
    for field in ("product", "version"):
        if not index.get(field):
            raise PackageError(f"the index has no {field!r}")
    if not index["boards"]:
        raise PackageError("the package declares no boards")
    if key is not None:
        # compare_digest: the check is cheap and the failure loud, but a
        # relabelled index is exactly what an attacker would iterate on.
        if not hmac_line or not hmac.compare_digest(
                hmac_line, index_hmac(body_text, key)):
            raise PackageError(
                "index HMAC does not verify -- the board labels may have been "
                "tampered with, and a mislabelled image cross-flashes a limb")
    return index
