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

  // ── Non-blocking reactor I/O (the fd must be O_NONBLOCK) ──────────────
  // One non-blocking write. Returns bytes accepted (0..len), 0 on would-block
  // (EAGAIN), or -1 on a broken link. Mirrors ::write; the FramedOutbox drains
  // through it and retries the remainder on the next POLLOUT.
  virtual long WriteSome(const std::uint8_t* data, std::size_t len) = 0;

  // One non-blocking read. Returns >0 bytes read, 0 on would-block (no data
  // ready yet — NOT end-of-stream), or -1 on EOF / a dead link. Distinct from
  // ReadNonblocking, whose 0 conflates EAGAIN with EOF (fine only behind poll).
  virtual long ReadSome(std::uint8_t* buf, std::size_t max_bytes) = 0;

  // Idempotent close.
  virtual void Close() = 0;
};

// pty-pair FdLink factory — for tests and the cross-language interop harness.
std::pair<std::shared_ptr<Link>, std::shared_ptr<Link>> MakeFdLinkPair();

// Wrap an existing fd (real serial port, socket, pty, ...). Takes ownership.
// `set_raw` is best-effort tty mode for pty/serial fds; ignored on others.
// `write_timeout_ms` > 0 bounds how long a blocking Write() waits for send-
// buffer space before returning false; 0 blocks indefinitely (ignored once
// `nonblocking`). `nonblocking` sets O_NONBLOCK so the reactor's WriteSome/
// ReadSome never block — the bus drains via poll() readiness instead.
std::shared_ptr<Link> MakeFdLink(int fd, bool set_raw = true,
                                 int write_timeout_ms = 0,
                                 bool nonblocking = false);

// Open a device path (e.g. "/dev/ttyGS0"), returning a Link or nullptr on
// failure. A reopenable SerialEndpoint calls this on each (re)connect; pass
// `nonblocking=true` for the reactor outbox path.
std::shared_ptr<Link> OpenFdLink(const char* path, bool set_raw = true,
                                 int write_timeout_ms = 0,
                                 bool nonblocking = false);

// Dial a TCP client connection to host:port, returning a Link or nullptr if the
// connect fails. TCP_NODELAY is set; `write_timeout_ms` bounds a stalled
// blocking write; `nonblocking` sets O_NONBLOCK for the reactor path.
std::shared_ptr<Link> OpenTcpClientLink(const char* host, std::uint16_t port,
                                        int write_timeout_ms = 0,
                                        bool nonblocking = false);

// Create a listening TCP socket bound to 0.0.0.0:port (SO_REUSEADDR), returning
// the listen fd or -1 on failure. Used by TcpServerEndpoint.
int OpenTcpListenSocket(std::uint16_t port);

// Set O_NONBLOCK on `fd`. Returns false if the fcntl get/set failed — the
// reactor treats that as fatal for a sink (a blocking fd would freeze the bus
// thread on a stalled peer).
bool SetNonblocking(int fd);

}  // namespace visio_schema::transport
