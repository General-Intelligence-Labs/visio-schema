#include "visio_schema/mcap/writer.hpp"

#include <cstdio>
#include <stdexcept>
#include <string>

// Vendored header-only mcap, lz4/zstd compiled out (we only ever use
// Compression::None) so this links with no extra deps and cross-compiles for
// the RV1106. MCAP_IMPLEMENTATION pulls the writer .inl into this single TU.
#define MCAP_COMPRESSION_NO_LZ4
#define MCAP_COMPRESSION_NO_ZSTD
#define MCAP_IMPLEMENTATION
#include <mcap/writer.hpp>

namespace visio_schema::mcap {

namespace {

::mcap::McapWriterOptions MakeOptions() {
  ::mcap::McapWriterOptions opts("");  // empty profile: plain protobuf channels
  opts.compression = ::mcap::Compression::None;
  return opts;
}

// Insert "_NNN" before the file extension: run.mcap -> run_000.mcap.
std::string NumberedPart(const std::string& path, std::size_t index) {
  char tag[8];
  std::snprintf(tag, sizeof(tag), "_%03zu", index);
  const std::size_t slash = path.find_last_of('/');
  const std::size_t dot = path.find_last_of('.');
  const bool has_ext = dot != std::string::npos &&
                       (slash == std::string::npos || dot > slash);
  if (!has_ext) return path + tag;
  return path.substr(0, dot) + tag + path.substr(dot);
}

}  // namespace

McapWriter::McapWriter(std::string_view path, std::uint64_t max_bytes,
                       double max_duration_s)
    : base_path_(path),
      max_bytes_(max_bytes),
      max_duration_ns_(static_cast<std::int64_t>(max_duration_s * 1e9)),
      rotating_(max_bytes > 0 || max_duration_s > 0.0) {
  OpenPart();
}

McapWriter::~McapWriter() {
  Close();
}

std::string McapWriter::PartPath() const {
  return rotating_ ? NumberedPart(base_path_, part_index_) : base_path_;
}

void McapWriter::OpenPart() {
  // Each part re-registers its own schemas/channels so it stands alone.
  schema_ids_.clear();
  channel_ids_.clear();
  part_bytes_ = 0;
  part_start_ = std::chrono::steady_clock::now();
  writer_ = std::make_unique<::mcap::McapWriter>();
  const std::string p = PartPath();
  const ::mcap::Status status = writer_->open(p, MakeOptions());
  if (!status.ok()) {
    throw std::runtime_error("McapWriter: cannot open " + p + ": " +
                             status.message);
  }
}

bool McapWriter::ShouldRoll() const {
  // Never roll an empty part (a stale duration would spin out empty files).
  if (part_bytes_ == 0) return false;
  if (max_bytes_ > 0 && part_bytes_ >= max_bytes_) return true;
  if (max_duration_ns_ > 0) {
    const auto elapsed = std::chrono::steady_clock::now() - part_start_;
    const auto ns =
        std::chrono::duration_cast<std::chrono::nanoseconds>(elapsed).count();
    if (ns >= max_duration_ns_) return true;
  }
  return false;
}

void McapWriter::Roll() {
  writer_->close();
  ++part_index_;
  OpenPart();
}

void McapWriter::Write(const Channel& channel, const Message& msg) {
  if (closed_) return;

  if (rotating_ && ShouldRoll()) Roll();

  auto sit = schema_ids_.find(channel.schema_name);
  if (sit == schema_ids_.end()) {
    // Schema.name is the protobuf full name; Schema.data is the
    // FileDescriptorSet, so Foxglove resolves the type inside it.
    ::mcap::Schema schema(channel.schema_name,
                        channel.schema_encoding.empty() ? kDefaultEncoding
                                                        : channel.schema_encoding,
                        channel.schema);
    writer_->addSchema(schema);
    sit = schema_ids_.emplace(channel.schema_name, schema.id).first;
  }

  auto cit = channel_ids_.find(channel.id);
  if (cit == channel_ids_.end()) {
    ::mcap::Channel ch(
        channel.topic,
        channel.encoding.empty() ? kDefaultEncoding : channel.encoding,
        sit->second);
    writer_->addChannel(ch);
    cit = channel_ids_.emplace(channel.id, ch.id).first;
  }

  const auto ts = static_cast<::mcap::Timestamp>(TimestampNs(msg.timestamp));
  ::mcap::Message out;
  out.channelId = cit->second;
  out.sequence = msg.seq;
  out.logTime = ts;
  out.publishTime = ts;
  out.dataSize = msg.payload.size();
  out.data = reinterpret_cast<const std::byte*>(msg.payload.data());
  writer_->write(out);
  part_bytes_ += msg.payload.size();
}

void McapWriter::Close() {
  if (closed_) return;
  closed_ = true;
  if (writer_) writer_->close();
}

}  // namespace visio_schema::mcap
