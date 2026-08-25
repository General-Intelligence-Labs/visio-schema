"""Generate the settings QR a fleet owner hands to their operators.

`visio-settings-qr` on the command line. Sealed by default: the secrets ride
in a `visio-seal-v1` envelope only a device can open, so the printed code is
safe to hand to the person it is least safe to trust with the credentials.

See `payload.py` for the wire form the companion app parses, `sealed_body.py`
for what goes inside the envelope, and `visio_schema.crypto.envelope` for the
envelope itself.
"""

from .payload import (
    MAX_BYTES,
    PAYLOAD_TYPE,
    PLAINTEXT_VERSION,
    SEALED_VERSION,
    WARN_BYTES,
    encode,
    validate,
)
from .sealed_body import (
    RECORDING_KEY_BYTES,
    EnvelopeTooLarge,
    SealedSecrets,
    open_sealed,
    recording_key_fingerprint,
    seal_into,
)

__all__ = [
    "MAX_BYTES",
    "PAYLOAD_TYPE",
    "PLAINTEXT_VERSION",
    "RECORDING_KEY_BYTES",
    "SEALED_VERSION",
    "WARN_BYTES",
    "EnvelopeTooLarge",
    "SealedSecrets",
    "encode",
    "open_sealed",
    "recording_key_fingerprint",
    "run",
    "seal_into",
    "validate",
]


def run() -> None:
    """Console-script entry point.

    Deferred so that importing this package for `validate`/`encode` alone —
    the app-fixture and visio-setup shape — does not pull in argparse and
    the CLI's own imports.
    """
    from .cli import run as _run
    _run()
