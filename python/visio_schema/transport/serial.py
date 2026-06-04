"""SerialEndpoint — COBS-delimited core-frames over a byte Link.

Per framing.md §3.2. The de/framing itself lives in :mod:`visio_schema.transport.framing`
(shared with the byte-stream reader and the examples); this endpoint just adds
the transport: a fixed :class:`Link`, an rx accumulator, and the
:class:`EndpointClosed` contract. It does NOT reconnect — a broken link raises,
and the caller decides what to do.
"""
from __future__ import annotations

from collections.abc import Iterable

from visio_schema.transport.endpoint import Endpoint, EndpointClosed
from visio_schema.transport.framing import extract_frames, frame_bytes
from visio_schema.transport.link import Link
from visio_schema.wire.message import Message


class SerialEndpoint(Endpoint):
    """COBS-framed core-frame Endpoint over a fixed byte :class:`Link`."""

    def __init__(self, link: Link) -> None:
        self._link = link
        self._rx_buf = bytearray()

    def fileno(self) -> int | None:
        return self._link.fileno()

    def try_read(self) -> Iterable[Message]:
        """Read whatever's available and yield any complete frames. Raises
        :class:`EndpointClosed` on EOF (the link won't reopen itself)."""
        try:
            chunk = self._link.read_nonblocking(4096)
        except BlockingIOError:
            return ()
        except OSError as exc:
            raise EndpointClosed(f"read failed: {exc}") from exc
        if not chunk:
            raise EndpointClosed("EOF on read")
        self._rx_buf.extend(chunk)
        return extract_frames(self._rx_buf)

    def write(self, msg: Message) -> None:
        try:
            self._link.write(frame_bytes(msg))
        except (BrokenPipeError, OSError) as exc:
            raise EndpointClosed(f"write failed: {exc}") from exc

    def close(self) -> None:
        self._link.close()
