// SerialEndpoint — FramedFdEndpoint over a CDC-ACM / serial fd, adding the
// CDC-ACM liveness watchdog (run from the endpoint's own I/O thread via Tick()).
//
// Ctors:
//   - inherited fixed-fd / factory ctors (no watchdog).
//   - SerialEndpoint(path, policy, usb_state): reopenable over a device path with
//     the SerialWatchdog (reopen on USB-CONFIGURED edge / drain-stall / retry).
//   - SerialEndpoint(factory, policy, usb_state): same, fd factory injected (test seam).
#pragma once

#include <functional>
#include <string>
#include <utility>

#include "visio_schema/transport/framed_fd.hpp"
#include "visio_schema/transport/link.hpp"  // FdFactory, OpenSerialFd
#include "visio_schema/transport/serial_watchdog.hpp"
#include "visio_schema/transport/write_policy.hpp"

namespace visio_schema::transport {

class SerialEndpoint : public FramedFdEndpoint {
 public:
  using UsbStateFn = std::function<std::string()>;

  using FramedFdEndpoint::FramedFdEndpoint;  // fixed-fd + factory ctors (no watchdog)

  explicit SerialEndpoint(std::string path,
                          WritePolicy policy = WritePolicy::drop_oldest(),
                          UsbStateFn usb_state = ReadUsbState);
  // Watchdog-driven with the fd factory + USB-state reader injected (test seam).
  SerialEndpoint(FdFactory factory, WritePolicy policy, UsbStateFn usb_state);

  // Read the kernel USB-gadget state from sysfs. "" on read failure. Injectable.
  static std::string ReadUsbState();

 protected:
  void Tick(std::int64_t now_ns) override;

 private:
  static FdFactory MakeFactory(std::string path) {
    return [path = std::move(path)]() { return OpenSerialFd(path.c_str()); };
  }

  bool watchdog_enabled_ = false;
  SerialWatchdog watchdog_;
  UsbStateFn usb_state_fn_;
};

}  // namespace visio_schema::transport
