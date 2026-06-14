// SerialEndpoint — FramedFdEndpoint over a CDC-ACM / serial fd.
//
// Two modes:
//   - Reopenable + watchdog (a stable LOCAL gadget node, e.g. /dev/ttyGS0): the
//     path/factory ctors run the SerialWatchdog from the I/O thread (reopen on
//     USB-CONFIGURED edge / drain-stall / retry).
//       - SerialEndpoint(path, policy, usb_state)
//       - SerialEndpoint(factory, policy, usb_state)   // fd factory injected (test seam)
//   - Non-reconnecting (a gated HOST-side child node, e.g. /dev/ttyACMn): the
//     fixed-fd ctor adopts an already-open fd with NO watchdog/reopen — a drop
//     fires on_closed so the owner re-discovers, instead of reopening a path
//     whose device-node number is unstable across reconnect.
//       - SerialEndpoint(fd, policy)
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

  // Non-reconnecting mode. Adopts an already-open, gated serial fd (e.g. one the
  // caller OpenSerialFd'd, read DeviceInfo from, and accepted) and frames over it
  // WITHOUT the watchdog/reopen: a drop fires on_closed for the owner to
  // re-discover, instead of silently reopening — required for host-side child
  // nodes (/dev/ttyACMn) whose number is unstable across reconnect, where
  // reopening the same path would hit the wrong device.
  explicit SerialEndpoint(int fd, WritePolicy policy = WritePolicy::drop_oldest())
      : FramedFdEndpoint(fd, policy) {}  // watchdog_enabled_ stays false

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
