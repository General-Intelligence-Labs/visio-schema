#!/usr/bin/env python3
"""Discover connected Visio devices for the ``visio-display --serve`` launcher.

Three transports, merged into one live list the launcher renders:

* :data:`USB` — a device on the USB CDC-ACM serial gadget, found by enumerating
  serial ports and keeping those with the GI-Labs USB vendor id (``0x2207``).
  Reached over the tty (``/dev/ttyACM*`` / ``/dev/cu.usbmodem*`` / ``COM*``), no IP.
* :data:`STA` — a device on the same Wi-Fi / LAN, found by browsing the
  ``_umi-protocol._tcp`` mDNS service it advertises; the SRV record carries the host
  and the Visio bus port (50001 on production units). Reached over TCP.
* :data:`AP` — a device in soft-AP fallback. A desktop can't portably scan/join
  Wi-Fi, so this is *manual*: once the operator has joined the ``GILABS-xxxx``
  hotspot, they add the device by ``host:port`` (default ``192.168.4.1:50001``) and
  we probe it.

Listing is deliberately **passive** — we never open the device's single-reader bus
to read its identity here, since that would starve the live bridge (a device's
serial / name would need a connection to learn, so listing shows the mDNS/USB label
only).

**NCM preference.** A USB-tethered device that runs the ``ncm_enabled`` gadget exposes
*both* a CDC-ACM serial leg (the lossy one) and a CDC-NCM USB-Ethernet leg carrying the
same TCP bus, so raw discovery sees it twice: a :data:`USB` row and an :data:`STA` row on
the per-device tether subnet (``10.<b0>.<b1>.2``; see the firmware's ``S41device-id``). Both
carry the unit's ``GILABS-<code8>`` (the USB ``product`` string and the mDNS instance name
are both the hostname), so they can be matched. The tether is a USB attachment, not Wi-Fi,
so :meth:`DiscoveryService.ui_snapshot` presents it **as a USB row that replaces the
CDC-ACM serial row** — the unit shows once, under USB, and connecting over it uses the
lossless NCM leg (real TCP retransmission over the cable). :meth:`DiscoveryService.resolve`
is the connect-side backstop: it maps a click on a still-listed serial row (a refresh race)
onto the NCM leg too. Plain (non-tether) Wi-Fi rows are untouched.

Discovery threads (zeroconf's own browser thread + our serial-poll thread) call
``on_change`` after any add/update/remove; a caller running an asyncio event loop
should marshal that onto the loop (e.g. ``loop.call_soon_threadsafe``).
"""
from __future__ import annotations

import re
import socket
import sys
import threading
from collections.abc import Callable
from pathlib import Path

# Transport tags — the closed set a device DTO's ``transport`` is drawn from.
USB = "usb"
STA = "sta"
AP = "ap"
# A local ``.mcap`` recording replayed as a source (not a live device).
MCAP = "mcap"
# MCAP file magic (spec §"Magic") — validate an added recording looks like one.
_MCAP_MAGIC = b"\x89MCAP0\r\n"

# GI-Labs USB vendor id — the CDC-ACM gadget on every ego / glove / gripper.
_GILABS_USB_VID = 0x2207
# The Visio bus mDNS service. The SRV record's port is authoritative (50001 on prod
# ego); the one-shot ``--tcp`` default of 9000 is a *different* preview listener.
_MDNS_SERVICE = "_umi-protocol._tcp.local."
# soft-AP fallback gateway + bus port (see wifi_manager.cpp start_ap()).
DEFAULT_AP_HOST = "192.168.4.1"
DEFAULT_BUS_PORT = 50001
# How often to re-enumerate serial ports (bounds appear/disappear latency).
_SERIAL_POLL_S = 1.5


def _usb_id(device: str) -> str:
    return f"usb:{device}"


def _tcp_id(host: str, port: int) -> str:
    return f"tcp:{host}:{port}"


def _device(*, dev_id: str, label: str, transport: str, host: str | None = None,
            port: int | None = None, device: str | None = None,
            path: str | None = None) -> dict:
    """Build a device DTO. ``id`` is a stable *connection* key (not the serial, which
    is unknown until we connect) — so the same unit reachable over both USB and Wi-Fi
    legitimately appears as two rows, each describing one way to reach it. ``path`` is
    set only for an :data:`MCAP` replay source (the local file to open)."""
    return {"id": dev_id, "label": label, "transport": transport,
            "host": host, "port": port, "device": device, "path": path}


# A unit's stable identity token — ``GILABS-<code8>`` — is stamped into BOTH its USB
# gadget ``product`` string and its mDNS instance name (both are the hostname; see the
# firmware's S41device-id / S50usbdevice). It is what lets a CDC-ACM serial row be matched
# to the same unit's NCM row. Match greedily to the token boundary so the USB label's
# trailing " (/dev/ttyACM0)" is excluded.
_DEVICE_KEY_RE = re.compile(r"GILABS-[0-9A-Za-z]+")


def _device_key(dto: dict) -> str | None:
    """The ``GILABS-<code8>`` identity shared by a unit's USB row and its mDNS row, or
    ``None`` if the label carries no such token (e.g. a manual host:port entry)."""
    m = _DEVICE_KEY_RE.search(dto.get("label") or "")
    return m.group(0) if m else None


def _local_source_ip(host: str) -> str | None:
    """The local source address the OS would use to reach ``host`` — a pure route lookup
    (a UDP ``connect`` sends no packet), so it's cheap and cross-platform. ``None`` if the
    host can't be resolved/routed."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))          # discard port; UDP connect transmits nothing
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _is_ncm_tether(host: str | None) -> bool:
    """Is ``host`` a device reached over its CDC-NCM USB-Ethernet tether, as opposed to
    Wi-Fi/LAN? S41device-id puts every NCM device on a private per-device /24
    (``10.<b0>.<b1>.2``, the USB host taking ``.1``), so the tether is a 10/8 address our
    own source address shares a /24 with — directly link-attached, not routed via a
    gateway. That same-subnet test doubles as a reachability guarantee, so redirecting a
    connect onto it can never strand us on an unreachable leg."""
    if not host or not host.startswith("10."):
        return False
    src = _local_source_ip(host)
    return bool(src) and src.rsplit(".", 1)[0] == host.rsplit(".", 1)[0]


class DiscoveryService:
    """Continuously discover devices across usb / sta / ap and notify on any change.

    Owns a zeroconf browser (its own thread) and a serial-poll thread. All device
    mutations are serialized by ``_lock``; ``on_change`` fires (from whichever
    discovery thread) after any add/update/remove. Degrades if a transport backend
    is unavailable (no network → no mDNS; no pyserial → no USB), warning to stderr so
    a genuinely broken/mis-bundled dependency is visible rather than silent."""

    def __init__(self, on_change: Callable[[], None] | None = None,
                 *, bus_port: int = DEFAULT_BUS_PORT) -> None:
        self._on_change = on_change
        self._bus_port = bus_port
        self._devices: dict[str, dict] = {}
        self._mdns_ids: dict[str, str] = {}   # mDNS instance name -> device id
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._serial_thread: threading.Thread | None = None
        self._serial_warned = False
        self._zc = None            # zeroconf.Zeroconf
        self._browser = None       # zeroconf.ServiceBrowser

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        self._start_mdns()
        self._serial_thread = threading.Thread(
            target=self._serial_loop, name="visio-serial-scan", daemon=True)
        self._serial_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._browser is not None:
            self._browser.cancel()
        if self._zc is not None:
            self._zc.close()
        if self._serial_thread is not None:
            self._serial_thread.join(timeout=2.0)

    def set_on_change(self, on_change: Callable[[], None] | None) -> None:
        """Set the change callback after construction — the launcher wires this once
        its event loop exists (the callback marshals onto that loop)."""
        self._on_change = on_change

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._devices.values())

    def ui_snapshot(self) -> list[dict]:
        """The device list as the launcher UI should render it. An NCM USB-Ethernet tether
        is a USB attachment, so it is presented as a :data:`USB` row that **replaces** the
        same unit's CDC-ACM serial row (matched by :func:`_device_key`): the unit appears
        once, under USB, and connecting over it uses the lossless NCM leg. The presented row
        keeps its own id (the tether ``tcp:`` id) so a click connects over NCM directly, and
        inherits the serial row's ``/dev/ttyACM*`` path so it still reads like the USB device
        it stands in for. Plain (non-tether) Wi-Fi rows and everything else pass through
        unchanged."""
        devices = self.snapshot()
        # key -> the tether STA dto (the leg to promote into the USB group). Test the cheap
        # key regex before the socket-touching tether probe (as resolve() does), so a keyless
        # STA row on the tether subnet doesn't pay a route lookup.
        tethers = {k: d for d in devices if d["transport"] == STA
                   and (k := _device_key(d)) is not None and _is_ncm_tether(d.get("host"))}
        if not tethers:
            return devices
        # /dev path of each overridden CDC-ACM row, to keep the promoted row familiar.
        acm_dev = {k: d.get("device") for d in devices
                   if d["transport"] == USB and (k := _device_key(d)) in tethers}
        out: list[dict] = []
        for d in devices:
            key = _device_key(d)
            if d["transport"] == USB and key in tethers:
                continue                                   # serial leg hidden — NCM stands in
            if tethers.get(key) is d:                      # this exact tether row → promote
                out.append({**d, "transport": USB, "device": acm_dev.get(key)})
            else:
                out.append(d)                              # Wi-Fi / other rows untouched
        return out

    def resolve(self, dev_id: str) -> dict | None:
        """Resolve a clicked device id to the DTO to actually connect over, applying the
        NCM preference: a CDC-ACM :data:`USB` row whose unit is *also* reachable over its
        CDC-NCM USB-Ethernet tether (same :func:`_device_key`, on the per-device tether
        subnet) resolves to that NCM row — the lossless TCP-over-USB leg in place of the
        lossy serial one. Every other row (already TCP, or with no NCM companion) resolves
        to itself. ``None`` if the id is unknown."""
        with self._lock:
            devices = list(self._devices.values())
        clicked = next((d for d in devices if d["id"] == dev_id), None)
        if clicked is None or clicked["transport"] != USB:
            return clicked
        key = _device_key(clicked)
        if key is None:
            return clicked
        # `and` short-circuits, so the (socket-touching) tether probe runs only for a
        # same-unit STA row — at most one.
        ncm = next((d for d in devices
                    if d["transport"] == STA and _device_key(d) == key
                    and _is_ncm_tether(d.get("host"))), None)
        if ncm is None:
            return clicked
        print(f"visio-display: {dev_id} → NCM tether {ncm['id']} "
              "(preferring lossless USB-Ethernet over CDC-ACM serial)", file=sys.stderr)
        return ncm

    # -- mutation ----------------------------------------------------------- #
    def _upsert(self, dto: dict) -> None:
        with self._lock:
            if self._devices.get(dto["id"]) == dto:
                return
            self._devices[dto["id"]] = dto
        self._notify()

    def _remove(self, dev_id: str) -> None:
        with self._lock:
            if dev_id not in self._devices:
                return
            del self._devices[dev_id]
        self._notify()

    def _notify(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except RuntimeError:
            pass  # event loop already closed during shutdown — nothing to notify

    # -- manual (AP) -------------------------------------------------------- #
    def add_manual(self, host: str, port: int | None = None) -> dict:
        """Add a device by ``host:port`` — the soft-AP / manual path. Probes
        reachability first (raises ``OSError`` on failure) so an unreachable entry is
        never shown as connectable."""
        port = port or self._bus_port
        with socket.create_connection((host, port), timeout=1.0):
            pass
        dto = _device(dev_id=_tcp_id(host, port), label=f"{host}:{port}",
                      transport=AP, host=host, port=port)
        self._upsert(dto)
        return dto

    # -- mcap replay (local file) ------------------------------------------ #
    def add_mcap(self, path: str) -> dict:
        """Add a local ``.mcap`` recording as a replay source. Reading the magic here
        means an obvious mistake (missing file, wrong file type) fails at add time —
        as an ``OSError`` the caller turns into a 400 — instead of mid-replay. The
        resolved path is the id, so adding the same file twice dedups to one row."""
        p = Path(path).expanduser().resolve()
        with open(p, "rb") as f:
            if f.read(len(_MCAP_MAGIC)) != _MCAP_MAGIC:
                raise OSError(f"not an MCAP recording: {p.name}")
        dto = _device(dev_id=f"mcap:{p}", label=p.name, transport=MCAP, path=str(p))
        self._upsert(dto)
        return dto

    # -- serial (USB) ------------------------------------------------------- #
    def _serial_loop(self) -> None:
        try:
            from serial.tools import list_ports
        except ImportError:
            print("visio-display: pyserial missing — USB discovery disabled",
                  file=sys.stderr)
            return
        while not self._stop.is_set():
            try:
                self._scan_serial(list_ports)
            except OSError as exc:
                if not self._serial_warned:      # warn once; don't spam every 1.5 s
                    self._serial_warned = True
                    print(f"visio-display: serial scan failed: {exc}", file=sys.stderr)
            self._stop.wait(_SERIAL_POLL_S)

    def _scan_serial(self, list_ports) -> None:
        seen: set[str] = set()
        for p in list_ports.comports():
            if p.vid != _GILABS_USB_VID:      # ListPortInfo always defines .vid (None if unknown)
                continue
            dev_id = _usb_id(p.device)
            seen.add(dev_id)
            product = p.product
            label = f"{product} ({p.device})" if product else f"USB {p.device}"
            self._upsert(_device(dev_id=dev_id, label=label,
                                 transport=USB, device=p.device))
        for dev_id in [d["id"] for d in self.snapshot()
                       if d["transport"] == USB and d["id"] not in seen]:
            self._remove(dev_id)

    # -- mDNS (Wi-Fi / STA) ------------------------------------------------- #
    def _start_mdns(self) -> None:
        try:
            from zeroconf import ServiceBrowser, Zeroconf
        except ImportError:
            print("visio-display: zeroconf missing — Wi-Fi discovery disabled",
                  file=sys.stderr)
            return
        try:
            self._zc = Zeroconf()
        except OSError:
            self._zc = None
            return  # no multicast / network → degrade to usb + manual
        self._browser = ServiceBrowser(self._zc, _MDNS_SERVICE, self._MdnsListener(self))

    def _on_mdns(self, zc, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=1500)
        if info is None:
            return
        addrs = info.parsed_addresses()
        if not addrs or not info.port:
            return
        host, port = addrs[0], info.port
        label = name.removesuffix("." + _MDNS_SERVICE) or host
        dev_id = _tcp_id(host, port)
        # If this instance previously resolved to a different address, drop the stale
        # row before adding the new one.
        prev = self._mdns_ids.get(name)
        if prev is not None and prev != dev_id:
            self._remove(prev)
        self._mdns_ids[name] = dev_id
        self._upsert(_device(dev_id=dev_id, label=label, transport=STA,
                             host=host, port=port))

    def _off_mdns(self, name: str) -> None:
        dev_id = self._mdns_ids.pop(name, None)
        if dev_id is not None:
            self._remove(dev_id)

    class _MdnsListener:
        """zeroconf ``ServiceListener`` — thin adapter onto the enclosing service."""

        def __init__(self, svc: DiscoveryService) -> None:
            self._svc = svc

        def add_service(self, zc, type_, name):
            self._svc._on_mdns(zc, type_, name)

        def update_service(self, zc, type_, name):
            # add_service already resolved this instance; re-resolving on every TTL
            # refresh would block the zeroconf callback thread in get_service_info
            # for no gain. A genuine address change re-announces (goodbye→hello =
            # remove→add), so only resolve instances we haven't seen.
            if name not in self._svc._mdns_ids:
                self._svc._on_mdns(zc, type_, name)

        def remove_service(self, zc, type_, name):
            self._svc._off_mdns(name)
