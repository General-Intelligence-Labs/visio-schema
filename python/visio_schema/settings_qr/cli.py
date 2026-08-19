"""`visio-settings-qr` — mint the settings QR a fleet owner hands to operators.

Sealed by default. `--plaintext` still emits the v1 form for pre-seal
firmware, but says loudly what that costs: a v1 code is a written-down
password, readable by anyone who photographs it.

    # seal a config to the published fleet key and print the code
    visio-settings-qr --config factory7.json --out factory7.png

    # first time keying a fleet: mint the recording key alongside the QR
    visio-settings-qr keygen --out factory7.key
    visio-settings-qr --config factory7.json --recording-key-file factory7.key \
        --first-provision --out factory7.png

    # rotate it later — proving you own the fleet
    visio-settings-qr --config factory7.json --recording-key-file new.key \
        --old-recording-key-file factory7.key --out rotate.png

    # what does this code actually say? (no private key needed)
    visio-settings-qr inspect factory7.payload.json
"""

from __future__ import annotations

import argparse
import json
import os
import secrets as pysecrets
import sys
import time
from pathlib import Path

from ..crypto import generate_keypair, key_id
from ..crypto.envelope import load_public_key_pem, public_key_pem
from .interactive import interactive
from .payload import (
    MAX_BYTES,
    META_FIELDS,
    PAYLOAD_TYPE,
    PLAINTEXT_VERSION,
    WARN_BYTES,
    encode,
    normalize_storage_prefix,
    validate,
)
from .sealed_body import (
    RECORDING_KEY_BYTES,
    EnvelopeTooLarge,
    SealedSecrets,
    recording_key_fingerprint,
    seal_into,
)

# Fields a v1 payload carries in the clear. `inspect` masks them by default
# so reading a code does not spray credentials into scrollback or CI logs.
_V1_SECRET_PATHS = (("storage", "secret_access_key"), ("wifi", "passphrase"))


class CliError(Exception):
    """A user-facing failure: one message, one exit code, no traceback."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def _read_hex_key(path: Path, want_bytes: int) -> bytes:
    """A hex key file -> raw bytes. Blank lines and `#` comments ignored.

    The comment tolerance is not decoration: every key file in
    `gilabs-root/secrets/` carries a "back this up" banner, and a tool that
    choked on it would push people toward stripping the warning. Exactly one
    hex line is allowed, though — joining several would silently splice two
    16-byte halves (or two different keys) into one accepted 32-byte key.
    """
    lines = [stripped for stripped in
             (line.split("#", 1)[0].strip() for line in
              path.read_text().splitlines()) if stripped]
    if len(lines) != 1:
        raise CliError(
            f"{path}: expected exactly one hex line, found {len(lines)}")
    try:
        raw = bytes.fromhex(lines[0])
    except ValueError as exc:
        raise CliError(f"{path}: not valid hex ({exc})") from exc
    if len(raw) != want_bytes:
        raise CliError(
            f"{path}: expected {want_bytes} bytes ({want_bytes * 2} hex "
            f"characters), got {len(raw)}")
    return raw


def _write_key_file(path: Path, raw: bytes, banner: str, force: bool) -> None:
    """Write a key 0600, refusing to clobber an existing one.

    Both guards matter. Without `O_EXCL` a second `keygen` in the same
    directory destroys the first key, whose own banner says there is no
    recovery path. And the `mode` argument to `os.open` applies only on
    CREATION, so overwriting a 0644 file would leave it world-readable —
    hence the explicit `fchmod`.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if not force:
        flags |= os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise CliError(
            f"{path} already exists — refusing to overwrite a key. Every "
            f"recording made under it would become unreadable. Pass --force "
            f"if you are certain.", code=2) from exc
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(banner)
        f.write(raw.hex() + "\n")


def _load_config(path: Path) -> dict:
    """Config file as a payload dict. Absent t/v are defaulted; wrong
    values are left in place for validate() to reject, never corrected."""
    try:
        cfg = json.loads(path.read_text())
    except OSError as exc:
        raise CliError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CliError(f"{path} is not valid JSON: {exc}", code=2) from exc
    if not isinstance(cfg, dict):
        raise CliError(f"{path}: config must be a JSON object", code=2)
    cfg.setdefault("t", PAYLOAD_TYPE)
    cfg.setdefault("v", PLAINTEXT_VERSION)
    return cfg


def _load_pubkey(path: Path | None) -> bytes | None:
    if path is None:
        return None
    try:
        return load_public_key_pem(path.read_bytes(), str(path))
    except (OSError, ValueError) as exc:
        raise CliError(f"cannot use {path} as a fleet public key: {exc}") from exc


def _collect_secrets(cfg: dict, args: argparse.Namespace) -> SealedSecrets:
    """Pull every secret out of the config and the CLI into one envelope."""
    storage = cfg.get("storage")
    storage_secret = None
    if isinstance(storage, dict):
        storage_secret = storage.get("secret_access_key") or None

    recording_key = None
    if args.clear_recording_key:
        recording_key = b""
    elif args.recording_key_file:
        recording_key = _read_hex_key(Path(args.recording_key_file),
                                      RECORDING_KEY_BYTES)

    old_key = None
    if args.old_recording_key_file:
        old_key = _read_hex_key(Path(args.old_recording_key_file),
                                RECORDING_KEY_BYTES)

    expires_at = None
    if args.expires_in is not None:
        expires_at = int(time.time()) + args.expires_in * 86400

    try:
        return SealedSecrets(
            storage_secret=storage_secret,
            recording_key=recording_key,
            old_recording_key=old_key,
            devices=tuple(args.device or ()),
            expires_at=expires_at,
        )
    except ValueError as exc:
        raise CliError(str(exc), code=2) from exc


def render_qr(payload: str, out: Path) -> None:
    # Imported here, not at module scope: it is the heaviest import in this
    # module and no other verb needs it.
    try:
        import qrcode
    except ImportError as exc:
        raise CliError(
            'rendering the QR needs qrcode — pip install "visio-schema[qr]"',
        ) from exc
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       border=4, box_size=10)
    qr.add_data(payload)
    qr.make(fit=True)
    try:
        qr.make_image(fill_color="black", back_color="white").save(out)
    except OSError as exc:
        raise CliError(f"cannot write {out}: {exc}") from exc
    print(f"QR version {qr.version} -> {out}", file=sys.stderr)


def _warn_partial_meta(cfg: dict) -> None:
    meta = cfg.get("meta")
    if isinstance(meta, dict) and len(meta) < len(META_FIELDS):
        # These four SetRecordingMeta fields carry no per-field presence
        # (unlike its fleet ids, which this payload does not set), so a
        # partial meta section clears the rest.
        missing = [f for f in META_FIELDS if f not in meta]
        print(f"note: meta fields {missing} are absent — the app will "
              "CLEAR them on the device", file=sys.stderr)


def _warn_unsealed_wifi(cfg: dict) -> None:
    """v2 seals the storage secret but NOT the Wi-Fi passphrase.

    Wi-Fi is applied over HTTP to the device web server rather than the bus,
    so sealing it is a separate seam (a deferred follow-up). Until then a
    sealed code can still carry a readable passphrase, and saying so is the
    difference between a known limit and a broken promise.
    """
    wifi = cfg.get("wifi")
    if isinstance(wifi, dict) and wifi.get("passphrase"):
        print("warning: wifi.passphrase is NOT sealed — it stays readable in "
              "this code. Sealing it needs firmware support that does not "
              "exist yet; omit the wifi section if that matters.",
              file=sys.stderr)


def _require_plaintext_allowed(secrets: SealedSecrets) -> None:
    if secrets.touches_recording_key:
        raise CliError(
            "a recording key cannot ride in a plaintext QR — that would "
            "print the key that protects the recordings next to the "
            "recordings. Drop --plaintext.", code=2)
    if secrets.devices or secrets.expires_at is not None:
        raise CliError(
            "--device/--expires-in are carried inside the sealed envelope, "
            "so they cannot be combined with --plaintext.", code=2)


def _require_rotation_proof(secrets: SealedSecrets,
                            first_provision: bool) -> None:
    """Rotating or clearing an existing key requires proving you set the
    last one. Catching it here means the mistake surfaces on a laptop
    rather than as `bad_old_key` on a rig in a factory."""
    if not secrets.touches_recording_key:
        return
    if secrets.old_recording_key is not None:
        return
    if secrets.clears_recording_key:
        raise CliError(
            "clearing the recording key needs --old-recording-key-file: the "
            "device requires the current key to stop encrypting.", code=2)
    if not first_provision:
        raise CliError(
            "setting a recording key needs --old-recording-key-file (the "
            "device requires the previous key to rotate). Pass "
            "--first-provision if this fleet has never been keyed.", code=2)


def cmd_qr(args: argparse.Namespace) -> int:
    if args.config:
        cfg = _load_config(Path(args.config))
    elif args.check_only:
        raise CliError("--check-only needs --config", code=2)
    else:
        try:
            cfg = interactive()
        except (KeyboardInterrupt, EOFError):
            raise CliError("aborted") from None

    normalize_storage_prefix(cfg)
    _warn_partial_meta(cfg)
    secrets = _collect_secrets(cfg, args)

    if args.plaintext:
        _require_plaintext_allowed(secrets)
        cfg["v"] = PLAINTEXT_VERSION
        payload_cfg = cfg
        if "wifi" in cfg or secrets.storage_secret:
            print("SECURITY: --plaintext embeds credentials in the code "
                  "itself. Anyone who photographs this QR has them. Use the "
                  "sealed default unless the target firmware predates it.",
                  file=sys.stderr)
    else:
        _require_rotation_proof(secrets, args.first_provision)
        _warn_unsealed_wifi(cfg)
        try:
            payload_cfg = seal_into(cfg, secrets,
                                    pubkey=_load_pubkey(
                                        Path(args.pubkey) if args.pubkey else None))
        except EnvelopeTooLarge as exc:
            raise CliError(str(exc), code=2) from exc
        except ValueError as exc:
            raise CliError(str(exc), code=2) from exc

    problems = validate(payload_cfg)
    if problems:
        raise CliError("invalid settings payload:\n"
                       + "\n".join(f"  - {p}" for p in problems), code=2)
    if args.check_only:
        print("ok", file=sys.stderr)
        return 0

    payload = encode(payload_cfg)
    size = len(payload.encode("utf-8"))
    if args.dry_run:
        # Print before the size gate — an oversized payload is exactly when
        # the user needs to see it to decide what to trim.
        print(payload)
        print(f"{size} bytes", file=sys.stderr)
        return 0
    if size > MAX_BYTES:
        raise CliError(f"payload is {size} B (> {MAX_BYTES} B) — too dense "
                       "to scan reliably; trim optional fields", code=2)
    if size > WARN_BYTES:
        print(f"warning: payload is {size} B — large codes scan slowly "
              "from small prints", file=sys.stderr)

    if secrets.sets_recording_key:
        print(f"recording key fingerprint: "
              f"{recording_key_fingerprint(secrets.recording_key)}",
              file=sys.stderr)
        print("NOTE: recordings made under a PREVIOUS key stay readable "
              "only with that key — keep every key you have ever used.",
              file=sys.stderr)

    render_qr(payload, Path(args.out))
    return 0


def cmd_keygen(args: argparse.Namespace) -> int:
    key = pysecrets.token_bytes(RECORDING_KEY_BYTES)
    out = Path(args.out)
    _write_key_file(out, key, (
        "# visio recording key — 32 bytes, lowercase hex.\n"
        "# Every .mcap sealed under it is UNREADABLE without this file.\n"
        "# Back it up somewhere you would keep a master password.\n"
    ), args.force)
    print(f"wrote {out} (0600)", file=sys.stderr)
    print(f"fingerprint: {recording_key_fingerprint(key)}", file=sys.stderr)
    print("LOSE THIS FILE AND EVERY RECORDING MADE UNDER IT IS PERMANENTLY "
          "UNREADABLE. There is no recovery path.", file=sys.stderr)
    return 0


def cmd_fleet_keygen(args: argparse.Namespace) -> int:
    private_raw, public_raw = generate_keypair()
    kid = key_id(public_raw)
    priv_out, pub_out = Path(args.out_private), Path(args.out_public)

    _write_key_file(priv_out, private_raw, (
        f"# visio-seal-v1 fleet PRIVATE key (X25519), kid {kid}.\n"
        f"# Baked into firmware images as /etc/visio_seal/{kid}.key.\n"
        "# BACK THIS UP: without it no future image can open any QR already\n"
        "# printed against this generation.\n"
    ), args.force)
    pub_out.write_bytes(
        f"# visio-seal-v1 fleet PUBLIC key (X25519).\n# kid: {kid}\n".encode()
        + public_key_pem(public_raw))
    print(f"kid {kid}: private -> {priv_out} (0600), public -> {pub_out}",
          file=sys.stderr)
    return 0


def _mask(cfg: dict) -> dict:
    """Blank the v1 cleartext credentials before printing a payload."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in cfg.items()}
    for section, key in _V1_SECRET_PATHS:
        block = out.get(section)
        if isinstance(block, dict) and block.get(key):
            block[key] = "<redacted, pass --reveal to show>"
    return out


def _read_payload(spec: str) -> str:
    """A payload argument -> its JSON text.

    Discriminated on shape, not on `Path.exists()`: a payload is a JSON
    object so it always starts with `{`, and probing the filesystem with a
    400-character literal both raises ENAMETOOLONG and silently reinterprets
    a mistyped filename as a payload instead of reporting the missing file.
    """
    if spec == "-":
        return sys.stdin.read()
    if spec.lstrip().startswith("{"):
        return spec
    try:
        return Path(spec).read_text()
    except OSError as exc:
        raise CliError(f"cannot read {spec}: {exc}") from exc


def cmd_inspect(args: argparse.Namespace) -> int:
    """Dump a payload's cleartext half. Needs no key — that is the point."""
    raw = _read_payload(args.payload)
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"not a settings payload: {exc}") from exc
    if not isinstance(cfg, dict):
        raise CliError("not a settings payload: expected a JSON object")

    sealed = cfg.pop("sealed", None)
    print(json.dumps(cfg if args.reveal else _mask(cfg), indent=2,
                     ensure_ascii=False))
    if sealed is None:
        print("\nsealed: (none) — every field above is in the clear",
              file=sys.stderr)
    else:
        print(f"\nsealed to fleet key {sealed.get('kid')}, "
              f"sets {sealed.get('has')}", file=sys.stderr)
        print("the values are readable only by a device holding that "
              "generation's private key", file=sys.stderr)
    for problem in validate({**cfg, **({"sealed": sealed} if sealed else {})}):
        print(f"  ! {problem}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="visio-settings-qr",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    qr = sub.add_parser("qr", help="generate a settings QR (the default)")
    qr.set_defaults(handler=cmd_qr)
    qr.add_argument("--config", metavar="JSON",
                    help="payload JSON file (omit for interactive prompts)")
    qr.add_argument("--out", metavar="PNG", default="settings_qr.png",
                    help="output QR image path (default: %(default)s)")
    qr.add_argument("--check-only", action="store_true",
                    help="validate --config and exit")
    qr.add_argument("--dry-run", action="store_true",
                    help="print the compact payload and exit (no PNG)")
    qr.add_argument("--plaintext", action="store_true",
                    help="emit the legacy v1 form with secrets in the clear "
                         "(for firmware that predates sealing)")
    qr.add_argument("--pubkey", metavar="PEM",
                    help="seal to this fleet public key instead of the one "
                         "shipped in this wheel")
    key_change = qr.add_mutually_exclusive_group()
    key_change.add_argument("--recording-key-file", metavar="FILE",
                            help="set the fleet's MCAP recording key from a "
                                 "hex key file (see `keygen`)")
    key_change.add_argument("--clear-recording-key", action="store_true",
                            help="stop encrypting new recordings (needs "
                                 "--old-recording-key-file)")
    qr.add_argument("--old-recording-key-file", metavar="FILE",
                    help="the key currently on the devices — required to "
                         "rotate or clear")
    qr.add_argument("--first-provision", action="store_true",
                    help="this fleet has never been keyed, so no "
                         "--old-recording-key-file exists yet")
    qr.add_argument("--device", metavar="SERIAL", action="append",
                    help="restrict this QR to a device serial (repeatable)")
    qr.add_argument("--expires-in", metavar="DAYS", type=int,
                    help="refuse to apply after DAYS (device clock)")

    kg = sub.add_parser("keygen", help="mint a 32-byte MCAP recording key")
    kg.set_defaults(handler=cmd_keygen)
    kg.add_argument("--out", metavar="FILE", default="recording.key",
                    help="key file to write, 0600 (default: %(default)s)")
    kg.add_argument("--force", action="store_true",
                    help="overwrite an existing key file")

    fk = sub.add_parser("fleet-keygen",
                        help="mint a fleet X25519 keypair (manufacturer only)")
    fk.set_defaults(handler=cmd_fleet_keygen)
    fk.add_argument("--out-private", metavar="FILE",
                    default="seal-private-key.hex")
    fk.add_argument("--out-public", metavar="FILE", default="fleet_key.pem")
    fk.add_argument("--force", action="store_true",
                    help="overwrite an existing private key file")

    ins = sub.add_parser("inspect",
                         help="show a payload's cleartext half (no key needed)")
    ins.set_defaults(handler=cmd_inspect)
    ins.add_argument("payload",
                     help="a payload file, a literal payload, or - for stdin")
    ins.add_argument("--reveal", action="store_true",
                     help="show v1 cleartext credentials instead of masking")
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `qr` is the default verb, so the historical
    # `gen_settings_qr.py --config x --out y` invocation keeps working.
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv.insert(0, "qr")
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
