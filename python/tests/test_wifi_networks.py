"""The multi-remembered-network surface: ForgetWifi and DeviceState.wifi_networks.

Order IS the payload here — the device tries `wifi_networks` front to back while
it is offline — so the round-trip tests below assert the sequence, not just the
membership.
"""
from __future__ import annotations

from visio_schema.v1.control import command_pb2, command_result_pb2


def test_forget_wifi_is_on_the_command_oneof_at_tag_33():
    """Tag 33 was the next free body tag; moving it would break fielded apps."""
    field = command_pb2.Command.DESCRIPTOR.fields_by_name["forget_wifi"]
    assert field.number == 33
    assert field.containing_oneof.name == "body"


def test_forget_wifi_roundtrip():
    c = command_pb2.Command(forget_wifi=command_pb2.ForgetWifi(ssid="site-wifi"))
    out = command_pb2.Command.FromString(c.SerializeToString())
    assert out.WhichOneof("body") == "forget_wifi"
    assert out.forget_wifi.ssid == "site-wifi"


def test_wifi_networks_preserves_order():
    """The list is newest-first and the device joins in that order, so a sort or
    a set anywhere in the path is a behaviour change, not a cosmetic one."""
    s = command_result_pb2.DeviceState(
        wifi_networks=[command_result_pb2.WifiNetwork(ssid=n)
                       for n in ("newest", "middle", "oldest")],
    )
    out = command_result_pb2.DeviceState.FromString(s.SerializeToString())
    assert [n.ssid for n in out.wifi_networks] == ["newest", "middle", "oldest"]


def test_wifi_networks_is_repeated_at_tag_30():
    field = command_result_pb2.DeviceState.DESCRIPTOR.fields_by_name["wifi_networks"]
    assert field.number == 30
    assert field.is_repeated
    assert field.message_type.name == "WifiNetwork"


def test_wifi_network_carries_no_credential():
    """The one test here that must never be relaxed.

    Credentials travel device-INBOUND only (ConnectWifi). Adding a passphrase —
    or anything else secret — to the outbound entry would leak every remembered
    PSK to any client that can read a CommandResult, over a bus with no auth.
    """
    assert set(command_result_pb2.WifiNetwork.DESCRIPTOR.fields_by_name) == {"ssid"}


def test_device_state_without_wifi_networks_parses_to_an_empty_list():
    """Firmware predating tag 30 sends nothing, and proto3 gives `repeated` no
    presence — so clients cannot distinguish "old firmware" from "nothing
    remembered" and must derive it from wifi_ssid instead. Pin the premise that
    derivation rests on: the field is absent, not an error."""
    old = command_result_pb2.DeviceState(wifi_state=1, wifi_ssid="site-wifi")
    out = command_result_pb2.DeviceState.FromString(old.SerializeToString())
    assert list(out.wifi_networks) == []
    assert out.wifi_ssid == "site-wifi"


def test_wifi_networks_wire_encoding_is_pinned():
    """Pin the exact bytes for the repeated field.

    `DeviceState.wifi_networks` is the first FT_POINTER field on an otherwise
    fully-inline `DeviceState`, and the device only ever ENCODES it — so a tag or
    wire-type slip would surface as garbage SSIDs on a phone, not as a build
    error. This pins the reference (libprotobuf) encoding.

    It does NOT prove nanopb agrees; that would need a vector in
    tests/golden/wire_vectors.txt, whose C++ side currently only encodes
    DeviceInfo. The nanopb side rides on WifiScanResults/RecordingsList, which
    are the identical `repeated message` + FT_POINTER-string construct and have
    been on the wire since 0.4.
    """
    s = command_result_pb2.DeviceState(
        wifi_networks=[command_result_pb2.WifiNetwork(ssid="a")],
    )
    # field 30, wire type 2 -> tag 0xf2 0x01; len 3; then WifiNetwork{ssid="a"}
    # = field 1, wire type 2 -> 0x0a, len 1, "a".
    assert s.SerializeToString() == bytes.fromhex("f201030a0161")
