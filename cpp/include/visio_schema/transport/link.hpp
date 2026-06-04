// Link — raw byte channel beneath Endpoints. A selector polls `Fileno()`; when
// readable, the Endpoint reads available bytes via `ReadNonblocking`. Writes
// block until the kernel accepts everything (optionally bounded by a timeout).
//
// Lives in visio-schema so a schema-only user can read/write one stream with no
// bus. Links do not reconnect; an Endpoint translates a broken link into an
// EndpointClosed the caller handles.
#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string_view>
#include <utility>

namespace visio_schema::transport {

class Link {
 public:
  virtual ~Link() = default;

  // Return the fd the bus's poll() should monitor.
  virtual int Fileno() const = 0;

  // Read whatever's available (up to max_bytes). Returns the actual byte count;
  // 0 means EOF / closed. Does NOT block; callers gate on poll() readability.
  virtual std::size_t ReadNonblocking(std::uint8_t* buf, std::size_t max_bytes) = 0;

  // Write all bytes. Returns false on broken pipe — or, when built with a
  // positive write timeout, if the kernel send buffer stays full past it (a
  // stalled peer). The owning Endpoint turns a false return into EndpointClosed.
  virtual bool Write(std::string_view data) = 0;

  // Idempotent close.
  virtual void Close() = 0;
};

// pty-pair FdLink factory — for tests and the cross-language interop harness.
std::pair<std::shared_ptr<Link>, std::shared_ptr<Link>> MakeFdLinkPair();

// Wrap an existing fd (real serial port, socket, pty, ...). Takes ownership.
// `set_raw` is best-effort tty mode for pty/serial fds; ignored on others.
// `write_timeout_ms` > 0 bounds how long a write waits for send-buffer space
// before returning false; 0 blocks indefinitely.
std::shared_ptr<Link> MakeFdLink(int fd, bool set_raw = true,
                                 int write_timeout_ms = 0);

// Open a device path (e.g. "/dev/ttyGS0"), returning a Link or nullptr on
// failure. The app opens one and wraps it in a SerialEndpoint; on disconnect it
// re-opens (endpoints don't self-reconnect). `write_timeout_ms` is forwarded.
std::shared_ptr<Link> OpenFdLink(const char* path, bool set_raw = true,
                                 int write_timeout_ms = 0);

// Dial a TCP client connection to host:port, returning a Link or nullptr if the
// connect fails. TCP_NODELAY is set; `write_timeout_ms` bounds a stalled write.
std::shared_ptr<Link> OpenTcpClientLink(const char* host, std::uint16_t port,
                                        int write_timeout_ms = 0);

// Create a listening TCP socket bound to 0.0.0.0:port (SO_REUSEADDR), returning
// the listen fd or -1 on failure. Used by TcpServerEndpoint.
int OpenTcpListenSocket(std::uint16_t port);

}  // namespace visio_schema::transport
