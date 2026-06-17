// McapWriter — write (Channel, Message) pairs to an MCAP file (the Foxglove
// container format). Mirrors python/visio_schema/mcap/writer.py:McapWriter.
//
// The canonical Visio MCAP writer, with no bus or resolver: the caller hands in
// the resolved Channel (topic + schema) and the Message. Payload bytes are
// stored verbatim (already-serialized protobuf). Schema/channel registration is
// lazy: one schema per schema_name, one channel per Channel.id.
//
// Schema naming: a protobuf channel's Schema.name is the payload's protobuf full
// name (Channel::schema_name) and Schema.data is its FileDescriptorSet
// (Channel::schema), so Foxglove resolves the type from the embedded set.
//
// Rotation: pass max_bytes and/or max_duration_s to split into self-contained
// numbered parts <stem>_0000<ext>, <stem>_0001<ext>, … max_bytes counts written
// payload bytes (approximate).
//
// Embeddable: the vendored header-only mcap writer is pulled into the .cc alone
// (lz4/zstd compiled out, Compression::None), and this header does NOT expose
// any mcap type, so consumers (and the visio McapEndpoint adapter) compile with
// no MCAP include and the whole sink cross-compiles for the RV1106.
#pragma once

#include <chrono>
#include <cstdint>
#include <memory>
#include <string>
#include <string_view>
#include <unordered_map>

#include "visio_schema/routing/channel.hpp"   // Channel
#include "visio_schema/wire/message.hpp"
#include "visio_schema/wire/time.hpp"          // TimestampNs

namespace mcap {
class McapWriter;
}

namespace visio_schema::mcap {

using visio_schema::wire::Message;

class McapWriter {
 public:
  // Record to a filesystem path. With max_bytes > 0 and/or max_duration_s > 0,
  // rotate into numbered parts (path becomes <stem>_NNN<ext>); 0 disables that
  // axis. Throws std::runtime_error if a part can't be opened.
  explicit McapWriter(std::string_view path, std::uint64_t max_bytes = 0,
                      double max_duration_s = 0.0);
  ~McapWriter();

  McapWriter(const McapWriter&) = delete;
  McapWriter& operator=(const McapWriter&) = delete;

  // Record one message against `channel` (lazily registering its schema +
  // channel records — declare-before-write).
  void Write(const Channel& channel, const Message& msg);
  void Close();

 private:
  std::string PartPath() const;
  void OpenPart();          // throws on open failure
  bool ShouldRoll() const;
  void Roll();
  // Close the current part and fsync it to physical media before moving on.
  void CloseCurrentPart();

  const std::string base_path_;
  const std::uint64_t max_bytes_;
  const std::int64_t max_duration_ns_;
  const bool rotating_;

  std::unique_ptr<::mcap::McapWriter> writer_;
  bool closed_ = false;
  std::size_t part_index_ = 0;
  std::uint64_t part_bytes_ = 0;
  std::chrono::steady_clock::time_point part_start_;

  // Caches (reset per part): schema id per schema_name, channel id per Channel id.
  std::unordered_map<std::string, std::uint16_t> schema_ids_;
  std::unordered_map<std::uint32_t, std::uint16_t> channel_ids_;
};

}  // namespace visio_schema::mcap
