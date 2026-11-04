"""Field primitives shared by the settings-QR validators.

`payload.py` validates the bucket-mode sections and `ingest.py` the Ingest API
ones; both report through `_err` and bound every text field through `_string`,
so the two cannot disagree about what "too long" or "required" means.
"""

from __future__ import annotations

from .i18n import tr


def _err(errs: list, where: str | None, key: str, **params: object) -> None:
    """Append one validation problem, translated to the active language.

    `where` is the WIRE field path (`storage.endpoint_url`) and is never
    translated — an operator matches it against the QR payload and the device
    docs, so it must read identically in every locale. Only the reason is
    prose. This is the split the app's parser draws too (settings-payload.ts).
    """
    reason = tr(key, **params)
    errs.append(f"{where}: {reason}" if where else reason)


def over_byte_limit(value: str, max_bytes: int) -> int | None:
    """`value`'s UTF-8 byte length if it overruns the device's decode buffer,
    else None. See DEVICE_MAX_SIZE for why an overrun is fatal, not truncating.
    One home so the validator and the interactive prompt cannot disagree."""
    nbytes = len(value.encode("utf-8"))
    return nbytes if nbytes > max_bytes else None


def _string(errs: list, where: str, v, *, required: bool = False,
            max_bytes: int | None = None, max_chars: int | None = None) -> str:
    # Every string field bounds a length; a call that sets neither would leave
    # the field silently uncapped, which is the very overflow this guards.
    assert max_bytes is not None or max_chars is not None, (
        f"_string({where!r}) set no length cap")
    if v is None:
        if required:
            _err(errs, where, "required")
        return ""
    if not isinstance(v, str):
        _err(errs, where, "mustBeString")
        return ""
    if required and v == "":
        _err(errs, where, "required")
    if max_chars is not None and len(v) > max_chars:
        _err(errs, where, "tooLongChars", max=max_chars)
    if max_bytes is not None:
        over = over_byte_limit(v, max_bytes)
        if over is not None:
            _err(errs, where, "tooLongBytes", bytes=over, max=max_bytes)
    return v


def _is_whole_number(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)  # bool is an int subclass


def _int_in_range(errs: list, where: str, v, lo: int, hi: int) -> None:
    if not _is_whole_number(v):
        _err(errs, where, "mustBeInteger")
        return
    if not lo <= v <= hi:
        _err(errs, where, "intRange", lo=lo, hi=hi)
