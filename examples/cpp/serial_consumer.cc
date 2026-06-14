// serial_consumer — minimal embedded-style consumer of live Visio serial.
//
// Reads COBS-delimited core frames (visio-schema/docs/protocol/framing.md §3.2) from a
// serial port, decodes each, and resolves it to a topic. This is the shape a
// Linux-class embedded board (e.g. an RV1106 gripper) would use: a blocking
// read loop, no bus, no threads, depending only on the single `visio_schema`
// target (nanopb bindings + framing codec + routing) — no libprotobuf, no
// abseil, the same code path that cross-compiles for the RV1106 / HDK.
//
// The wire Header addresses every message by a compact `stream_id` (control ids
// < CONTROL_STREAM_FIRST_DYNAMIC are hop-local; data ids are negotiated and
// hub-remapped). A `ChannelRegistry` learns the periodic DeviceInfo announce and
// resolves each data frame to its topic + schema — exactly what a customer needs
// to consume a Visio device, with nothing from the runtime layer.
//
//   build:  cmake -S examples/cpp -B examples/cpp/build && cmake --build examples/cpp/build
//   run:    ./examples/cpp/build/serial_consumer /dev/ttyUSB0
//
// Pair it with a loopback for a quick test:
//   socat -d -d pty,raw,echo=0 pty,raw,echo=0
//   ./serial_consumer /dev/pts/N      # then feed frames into the other pty

#include <fcntl.h>
#include <termios.h>
#include <unistd.h>

#include <cstdint>
#include <cstdio>
#include <iostream>
#include <string>
#include <string_view>
#include <vector>

#include "visio_schema/routing/registry.hpp"
#include "visio_schema/transport/framing.hpp"
#include "visio_schema/v1/wire/header.pb.h"

namespace {

// Open `path` read-only and put it in raw mode at `baud`. Returns the fd, or
// -1 on error (errno set).
int OpenSerial(const char* path, speed_t baud) {
  const int fd = ::open(path, O_RDONLY | O_NOCTTY);
  if (fd < 0) return -1;
  termios tio{};
  if (::tcgetattr(fd, &tio) != 0) {
    ::close(fd);
    return -1;
  }
  ::cfmakeraw(&tio);
  ::cfsetispeed(&tio, baud);
  ::cfsetospeed(&tio, baud);
  tio.c_cc[VMIN] = 1;   // block until at least one byte arrives
  tio.c_cc[VTIME] = 0;  // no inter-byte timeout
  if (::tcsetattr(fd, TCSANOW, &tio) != 0) {
    ::close(fd);
    return -1;
  }
  return fd;
}

}  // namespace

int main(int argc, char** argv) {
  const char* path = (argc > 1) ? argv[1] : "/dev/ttyGS0";

  const int fd = OpenSerial(path, B921600);
  if (fd < 0) {
    std::perror("open serial");
    return 1;
  }
  std::cerr << "reading visio frames from " << path << " (Ctrl-C to stop)\n";

  // Learns DeviceInfo announces and resolves each data frame's stream_id to its
  // topic + schema — the whole self-contained consume path, no bus.
  visio_schema::routing::ChannelRegistry registry;
  constexpr std::uint32_t kDeviceInfo =
      visio_schema_v1_wire_ControlStream_CONTROL_STREAM_DEVICE_INFO;

  std::vector<std::uint8_t> rx;  // byte accumulator across reads
  std::uint8_t chunk[4096];
  while (true) {
    const ssize_t n = ::read(fd, chunk, sizeof(chunk));
    if (n < 0) {
      std::perror("read");
      break;
    }
    if (n == 0) break;  // EOF (peer closed)
    rx.insert(rx.end(), chunk, chunk + n);

    // The shared de/framer pulls every complete 0x00-delimited frame out of the
    // accumulator, COBS+frame-decodes each (malformed frames skipped), and
    // leaves a partial trailing frame for the next read.
    for (auto& msg : visio_schema::transport::ExtractFrames(rx)) {
      const std::uint32_t sid = msg.stream_id;
      auto routed = registry.Accept(std::move(msg));
      if (routed.channel != nullptr) {
        // Resolved data frame: stream_id -> topic + payload type.
        std::cout << "data  " << routed.channel->topic << "  ["
                  << routed.channel->schema_name << "]  seq="
                  << routed.message->seq << "  payload="
                  << routed.message->payload.size() << "B\n";
      } else if (routed.message.has_value()) {
        std::cout << "ctrl  stream_id=" << sid << "  payload="
                  << routed.message->payload.size() << "B\n";
      } else if (sid == kDeviceInfo) {
        std::cout << "learned announce (" << registry.Channels().size()
                  << " channels known)\n";
      }
      // else: data frame dropped-until-mapped (announce not seen yet).
    }
  }

  ::close(fd);
  return 0;
}
