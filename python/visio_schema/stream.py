"""Read a live device as resolved ``(message, channel)`` rows — the live twin of `read_mcap`.

`read_serial` opens a serial port, de-frames the COBS wire stream, and runs it through a
`ChannelRegistry` so a consumer gets the same ``(message, channel)`` rows a recording yields — no
callbacks, no manual registry. Use this for read-only viewing/recording; for the bidirectional
path (sending commands back), use `serial_endpoint` and its ``send``.
"""
from __future__ import annotations

import select
from collections.abc import Iterator

from visio_schema.routing import Channel, ChannelRegistry
from visio_schema.transport import close_fd, open_serial_fd, read_frames, read_some
from visio_schema.wire.message import Message

_READ_CHUNK = 65536


def _read_chunks(fd: int) -> Iterator[bytes]:
    """Yield bytes from a non-blocking ``fd``, waiting for readability so an idle link
    doesn't busy-spin. Stops on EOF / a dead fd. The caller owns and closes ``fd``.

    Uses ``select.select`` rather than ``selectors.DefaultSelector``: on macOS the
    default selector is kqueue, whose ``EVFILT_READ`` does not fire for a pty/tty (a
    documented BSD limitation) — a serial fd would then look permanently idle and the
    reader would hang forever (the 0.5 s timeout keeps returning empty). ``select(2)``
    reports tty/pty readability correctly on every platform (it's what the transport's
    ``framed_fd`` writer loop already uses)."""
    while True:
        r, _w, _x = select.select([fd], [], [], 0.5)  # idle tick — lets KeyboardInterrupt land
        if not r:
            continue
        chunk = read_some(fd, _READ_CHUNK)
        if chunk is None:                 # EOF / dead fd
            return
        if chunk:
            yield chunk


def read_serial(port: str) -> Iterator[tuple[Message, Channel]]:
    """Read a live device as resolved ``(message, channel)`` rows.

    Opens the serial port, de-frames the COBS wire stream, and resolves each data
    message against the device's `DeviceInfo` announces — so you get the same rows
    `read_mcap` yields from a recording, and live/replay code is identical. Announces
    are learned and absorbed; only mapped data messages are produced. This is the
    simple, read-only path; to also send to the device, use `serial_endpoint`.

    Args:
        port: Serial device path, e.g. ``"/dev/ttyACM0"``.

    Yields:
        ``(message, channel)`` tuples — a `Message` (header fields + payload bytes)
        paired with the `Channel` (topic + schema) it was published on.

    Raises:
        OSError: If `port` cannot be opened.

    The port is closed when the generator is exhausted or closed (e.g. on ``break``).

    Example:
        for msg, channel in read_serial("/dev/ttyACM0"):
            print(channel.topic, msg.seq, len(msg.payload))
    """
    fd = open_serial_fd(port)
    if fd < 0:
        raise OSError(f"could not open serial port {port!r}")
    try:
        yield from ChannelRegistry().resolved(read_frames(_read_chunks(fd)))
    finally:
        close_fd(fd)
