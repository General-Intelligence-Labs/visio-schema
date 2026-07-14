// fd byte I/O — the raw file-descriptor layer beneath endpoints. There is no Link
// object: the fd IS the link. An endpoint owns one `int fd_` and does its own
// non-blocking poll/read/write through these free functions; a reopenable endpoint
// gets a fresh fd from an FdFactory (std::function<int()>) on each reconnect.
//
// Lives in visio-schema so a schema-only user can read/write one stream with no
// bus. All fds are O_NONBLOCK: WriteSome/ReadSome never block — callers gate on
// poll() readiness instead.
#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <utility>

namespace visio_schema::transport {

// A source of fresh, connected fds for a reopenable endpoint. Returns -1 on a
// failed (re)connect. Called now and on each reconnect.
using FdFactory = std::function<int()>;

// Set O_NONBLOCK on `fd`. Returns false if the fcntl get/set failed — an endpoint
// treats that as fatal (a blocking fd would freeze its I/O thread on a stalled peer).
bool SetNonblocking(int fd);

// Best-effort raw tty mode (cfmakeraw) on `fd`. No-op (ENOTTY) on non-ttys.
// OpenSerialFd applies it; pty-based test/interop helpers call it on the master.
void SetRawMode(int fd);

// One non-blocking write. Returns bytes accepted (0..len), 0 on would-block
// (EAGAIN), or -1 on a broken fd. Mirrors ::write; the outbox drains through it
// and retries the remainder on the next POLLOUT.
long WriteSome(int fd, const std::uint8_t* data, std::size_t len);

// One non-blocking read. Returns >0 bytes read, 0 on would-block (no data ready
// yet — NOT end-of-stream), or -1 on EOF / a dead fd.
long ReadSome(int fd, std::uint8_t* buf, std::size_t max_bytes);

// Close `fd` (no-op if < 0). Flushes any queued TX first (tcflush TCOFLUSH) so a
// CDC-ACM gadget close doesn't block when no host is reading; no-op (ENOTTY) on
// sockets / regular fds.
void CloseFd(int fd);

// Open a device path (e.g. "/dev/ttyGS0") raw + non-blocking. Returns the fd or
// -1 on failure. A reopenable SerialEndpoint's factory calls this each reconnect.
int OpenSerialFd(const char* path);

// Dial a TCP client to host:port (TCP_NODELAY + SO_KEEPALIVE, non-blocking).
// Returns the fd or -1 if the connect fails.
// Connect a TCP socket to host:port, returning a non-blocking fd (or -1). With
// timeout_ms > 0 the connect itself is bounded (non-blocking connect + poll) so a
// dropped SYN fails in timeout_ms instead of the ~127 s kernel SYN-retry; 0 (the
// default) keeps the historical blocking connect.
int DialTcpFd(const char* host, std::uint16_t port, int timeout_ms = 0);

// A connected pair of raw, non-blocking fds (socketpair). For tests and the
// cross-language interop harness: one end drives an endpoint, the other is
// read/written directly. Returns {-1,-1} on failure.
std::pair<int, int> MakeFdPair();

// Create a listening TCP socket bound to 0.0.0.0:port (SO_REUSEADDR), returning
// the listen fd or -1 on failure. Used by TcpAcceptor.
int OpenTcpListenSocket(std::uint16_t port);

// Accept one pending connection on `listen_fd` with FD_CLOEXEC set on the new fd
// (and SIGPIPE suppressed where the platform needs a socket option rather than a
// send() flag). Returns the client fd, or -1 on EAGAIN / error. Portable stand-in
// for Linux's accept4(SOCK_CLOEXEC).
int AcceptCloexec(int listen_fd);

}  // namespace visio_schema::transport
