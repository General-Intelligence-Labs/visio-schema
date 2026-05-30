// serial_consumer — minimal embedded-style consumer of live Visio serial.
//
// Reads COBS-delimited core frames (visio-schema/docs/framing.md §3.2) from a
// serial port, decodes each Header, and prints it. This is the shape a
// Linux-class embedded board (e.g. an RV1106 gripper) would use: a blocking
// read loop, no bus, no threads, depending only on the single `visio_schema`
// target (generated bindings + framing codec).
//
// A true bare-metal MCU would swap the generated libprotobuf bindings for
// nanopb, but the COBS / CRC / frame-split logic below is identical.
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

#include "visio/sensor/v1/imu_raw.pb.h"
#include "visio/wire/codec/cobs.hpp"
#include "visio/wire/codec/frame.hpp"
#include "visio/wire/v1/header.pb.h"

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
  GOOGLE_PROTOBUF_VERIFY_VERSION;
  const char* path = (argc > 1) ? argv[1] : "/dev/ttyGS0";

  const int fd = OpenSerial(path, B921600);
  if (fd < 0) {
    std::perror("open serial");
    return 1;
  }
  std::cerr << "reading visio frames from " << path << " (Ctrl-C to stop)\n";

  std::string rx;  // byte accumulator across reads
  std::uint8_t chunk[4096];
  while (true) {
    const ssize_t n = ::read(fd, chunk, sizeof(chunk));
    if (n < 0) {
      std::perror("read");
      break;
    }
    if (n == 0) break;  // EOF (peer closed)
    rx.append(reinterpret_cast<const char*>(chunk), static_cast<std::size_t>(n));

    // Each frame ends at a 0x00 delimiter; COBS guarantees no other 0x00 in
    // the encoded run, so we can split on it.
    std::size_t delim;
    while ((delim = rx.find('\0')) != std::string::npos) {
      const std::string encoded = rx.substr(0, delim);
      rx.erase(0, delim + 1);
      if (encoded.empty()) continue;  // bare delimiter / empty frame

      std::vector<std::uint8_t> decoded;
      if (!visio::wire::CobsDecode(encoded, &decoded)) {
        std::cerr << "drop: COBS decode failed (" << encoded.size() << " B)\n";
        continue;
      }
      visio::wire::v1::Header header;
      std::string payload;
      const std::string_view frame(
          reinterpret_cast<const char*>(decoded.data()), decoded.size());
      const auto status = visio::wire::DecodeFrame(frame, &header, &payload);
      if (status != visio::wire::FrameStatus::kOk) {
        std::cerr << "drop: " << visio::wire::FrameStatusName(status) << "\n";
        continue;
      }

      std::cout << "device=" << header.device()
                << " stream=" << header.stream()
                << " idx=" << header.stream_index()
                << " seq=" << header.seq()
                << " payload=" << payload.size() << "B";
      // Demo: actually parse one stream type to show the payload decodes.
      if (header.stream() == visio::wire::v1::STREAM_IMU_RAW) {
        visio::sensor::v1::ImuRaw imu;
        if (imu.ParseFromString(payload)) {
          std::cout << " imu_samples=" << imu.samples_size();
        }
      }
      std::cout << '\n';
    }
  }

  ::close(fd);
  return 0;
}
