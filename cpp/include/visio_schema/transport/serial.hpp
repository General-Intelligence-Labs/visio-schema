// SerialEndpoint — COBS-delimited core-frames over a CDC-ACM / serial Link.
// A subclass of FramedFdEndpoint (framing + outbox live there) that adds the
// CDC-ACM liveness watchdog.
//
// Ctors:
//   - the inherited fixed-link / factory ctors (FramedFdEndpoint) — no watchdog.
//   - SerialEndpoint(path, policy, usb_state): reopenable over a device path with
//     the SerialWatchdog. The fd is opened O_NONBLOCK; OnTick() reads the USB
//     gadget state + the outbox's pending bytes and reopens /dev/ttyGS0 on a
//     CONFIGURED edge (host re-enumerated), a drain stall (host closed its TTY),
//     or a periodic retry while the link is down. This is the CDC-ACM auto-resume
//     the original firmware relied on (no reliable host-detached signal exists).
#pragma once

#include <functional>
#include <string>
#include <utility>

#include "visio_schema/transport/framed_fd.hpp"
#include "visio_schema/transport/link.hpp"
#include "visio_schema/transport/serial_watchdog.hpp"
#include "visio_schema/transport/write_policy.hpp"

namespace visio_schema::transport {

class SerialEndpoint : public FramedFdEndpoint {
 public:
  // Returns the USB gadget state ("CONFIGURED"/.../"" if unreadable).
  using UsbStateFn = std::function<std::string()>;

  using FramedFdEndpoint::FramedFdEndpoint;  // fixed-link + factory ctors (no watchdog)

  // Reopenable over a device path, driven by the SerialWatchdog.
  explicit SerialEndpoint(std::string path,
                          WritePolicy policy = WritePolicy::drop_oldest(),
                          UsbStateFn usb_state = ReadUsbState);

  // Watchdog-driven, but with the link factory + USB-state reader injected — the
  // host-test seam (the path ctor delegates here with the real openers).
  SerialEndpoint(LinkFactory factory, WritePolicy policy, UsbStateFn usb_state);

  void OnTick(std::int64_t now_ns) override;

  // Read the kernel USB-gadget state from sysfs (android_usb, then UDC). Returns
  // "" on read failure. Exposed so callers can inject a fake in tests.
  static std::string ReadUsbState();

 private:
  static LinkFactory MakeFactory(std::string path) {
    return [path = std::move(path)]() {
      return OpenFdLink(path.c_str(), /*set_raw=*/true, /*write_timeout_ms=*/0,
                        /*nonblocking=*/true);
    };
  }

  bool watchdog_enabled_ = false;
  SerialWatchdog watchdog_;
  UsbStateFn usb_state_fn_;
};

}  // namespace visio_schema::transport
