"""Host Wi-Fi discovery is shared without importing the display web server."""

from types import SimpleNamespace

import pytest

from visio_schema import host_wifi


@pytest.mark.parametrize(
    "platform,method",
    [("linux", "_scan_linux"), ("win32", "_scan_windows"), ("darwin", "_scan_macos")],
)
def test_dispatches_to_host_platform(monkeypatch, platform, method):
    networks = [dict(ssid="Station", signal=90, security="WPA2")]
    monkeypatch.setattr(host_wifi.sys, "platform", platform)
    monkeypatch.setattr(host_wifi, method, lambda: networks)
    assert host_wifi.scan_host_wifi() == networks


def test_linux_scanner_deduplicates_and_sorts_escaped_ssids(monkeypatch):
    sample = "Weak:40:WPA2\nAP\\:Strong:95:WPA2\n:100:OPEN\nWeak:70:WPA2\n"
    monkeypatch.setattr(
        host_wifi.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(stdout=sample)
    )
    assert host_wifi._scan_linux() == [
        dict(ssid="AP:Strong", signal=95, security="WPA2"),
        dict(ssid="Weak", signal=70, security="WPA2"),
    ]


def test_host_scan_does_not_import_optional_display_server():
    assert host_wifi.scan_host_wifi.__module__ == "visio_schema.host_wifi"
