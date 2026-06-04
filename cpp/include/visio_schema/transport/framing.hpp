// COBS-delimited core-frame helpers shared by the fd-backed endpoints
// (SerialEndpoint / TcpEndpoint / TcpServerEndpoint) and the examples. Per
// framing.md §3.2: each frame is CobsEncode(EncodeFrame(msg)) followed by a
// 0x00 delimiter. The single C++ implementation of the de/framing loop.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "visio_schema/transport/link.hpp"
#include "visio_schema/wire/message.hpp"

namespace visio_schema::transport {

using visio_schema::wire::Message;

// Pull every complete (0x00-delimited) frame out of `rx_buf`, COBS+frame-decode
// each to a Message, and return them. Consumed bytes are erased from the front;
// a partial trailing frame is left for the next call. Malformed frames skipped.
std::vector<Message> ExtractFrames(std::vector<std::uint8_t>& rx_buf);

// Frame + COBS-encode `msg` into the on-wire byte sequence (with the trailing
// 0x00 delimiter). The reactor sinks enqueue this into their outbox; WriteFramed
// is the synchronous convenience wrapper around it.
std::vector<std::uint8_t> EncodeFramed(const Message& msg);

// Frame + COBS-encode `msg` (with the trailing 0x00) and write it to `link`.
// Returns Link::Write()'s result (false = broken/stalled link).
bool WriteFramed(Link& link, const Message& msg);

}  // namespace visio_schema::transport
