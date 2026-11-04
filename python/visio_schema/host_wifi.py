"""Host Wi-Fi discovery shared by operator tools; no device connection or web server."""
from __future__ import annotations

import re
import subprocess
import sys


def scan_host_wifi() -> list[dict]:
    """Scan for Wi-Fi networks visible to THIS host (the operator's PC), so they can pick
    one to provision onto the device. We scan host-side, NOT via the device's ScanWifi,
    because the device usually can't scan while it's serving its AP fallback — and the
    PC is co-located with the device, so it sees the same networks the device could join.
    Returns ``[{ssid, signal(0-100), security}]`` strongest-first; raises if no host
    scanner is available, so callers can report the failure."""
    if sys.platform == "darwin":
        return _scan_macos()
    if sys.platform == "win32":
        return _scan_windows()
    return _scan_linux()


def _dedup_strongest(nets: list[dict]) -> list[dict]:
    """One row per SSID, keeping the strongest signal, sorted strongest-first."""
    best: dict[str, dict] = {}
    for n in nets:
        if not n["ssid"]:
            continue   # hidden network
        cur = best.get(n["ssid"])
        if cur is None or n["signal"] > cur["signal"]:
            best[n["ssid"]] = n
    return sorted(best.values(), key=lambda n: -n["signal"])


def _scan_linux() -> list[dict]:
    # nmcli -t: terse, ':'-separated, ':' inside a field escaped as '\:'. SIGNAL is 0-100.
    out = subprocess.run(
        ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"],
        capture_output=True, text=True, timeout=20, check=True)
    nets = []
    for line in out.stdout.splitlines():
        fields = [f.replace("\\:", ":") for f in re.split(r"(?<!\\):", line)]
        if len(fields) < 3:
            continue
        ssid, signal, security = fields[0], fields[1], fields[2]
        nets.append({"ssid": ssid, "signal": int(signal) if signal.isdigit() else 0,
                     "security": security or "OPEN"})
    return _dedup_strongest(nets)


def _scan_windows() -> list[dict]:
    # `netsh wlan show networks mode=bssid` groups: "SSID N : name", then "Authentication :
    # ...", then per-BSSID "Signal : NN%". Take the strongest Signal seen under each SSID.
    out = subprocess.run(["netsh", "wlan", "show", "networks", "mode=bssid"],
                         capture_output=True, text=True, timeout=20, check=True)
    nets, cur = [], None
    for raw in out.stdout.splitlines():
        line = raw.strip()
        if line.startswith("SSID ") and " : " in line:
            cur = {"ssid": line.split(" : ", 1)[1].strip(), "signal": 0, "security": "OPEN"}
            nets.append(cur)
        elif cur is not None and line.startswith("Authentication"):
            cur["security"] = line.split(":", 1)[1].strip()
        elif cur is not None and line.startswith("Signal"):
            pct = line.split(":", 1)[1].strip().rstrip("%")
            cur["signal"] = max(cur["signal"], int(pct) if pct.isdigit() else 0)
    return _dedup_strongest(nets)


def _scan_macos() -> list[dict]:
    # The private `airport -s` CLI was removed in macOS 14+, so parse
    # `system_profiler SPAirPortDataType` (no sudo). Under "Other Local Wi-Fi Networks:"
    # each network is an indented "SSID:" block with "Security:" and "Signal / Noise:".
    out = subprocess.run(["system_profiler", "SPAirPortDataType"],
                         capture_output=True, text=True, timeout=25, check=True)
    lines = out.stdout.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip() == "Other Local Wi-Fi Networks:"), None)
    if start is None:
        return []
    base = len(lines[start]) - len(lines[start].lstrip())
    nets, cur = [], None
    for line in lines[start + 1:]:
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= base:
            break   # dedented out of the section
        s = line.strip()
        if s.endswith(":") and ": " not in s:            # a network name ("MySSID:")
            cur = {"ssid": s[:-1], "signal": 0, "security": "OPEN"}
            nets.append(cur)
        elif cur is not None and s.startswith("Security:"):
            cur["security"] = s.split(":", 1)[1].strip()
        elif cur is not None and s.startswith("Signal / Noise:"):
            m = re.search(r"(-?\d+)\s*dBm", s)
            if m:
                cur["signal"] = max(0, min(100, 2 * (int(m.group(1)) + 100)))   # dBm→~%
    return _dedup_strongest(nets)
