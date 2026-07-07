#include "visio_schema/routing/registry.hpp"

#include <pb_decode.h>
#include <pb_encode.h>

#include <cstring>
#include <iostream>
#include <stdexcept>
#include <utility>

#include "visio_schema/v1/service/device_info/device_info.pb.h"
#include "visio_schema/wire/schema_blobs.gen.hpp"
#include "visio_schema/v1/wire/header.pb.h"

namespace visio_schema::routing {

namespace {

// Well-known channel for the DeviceInfo control stream (see device_info_channel_).
constexpr const char* kDeviceInfoTopic = "/device_info";
constexpr const char* kDeviceInfoSchema =
    "visio_schema.v1.service.device_info.DeviceInfo";

// nanopb POINTER string: NULL omits the field; non-NULL (even empty) encodes it.
char* OrNull(const std::string& s) {
  return s.empty() ? nullptr : const_cast<char*>(s.c_str());
}

}  // namespace

// ── DeviceInfo encode / decode (nanopb FT_POINTER) ──────────────────────────

std::string ChannelRegistry::Encode(const std::string& device_name,
                                    const std::string& equipment_type,
                                    const std::string& firmware_version,
                                    const std::string& hardware_revision,
                                    const std::string& serial,
                                    std::uint64_t boot_unix_seconds,
                                    const std::vector<Channel>& channels) {
  std::vector<std::vector<std::uint8_t>> schema_blobs(channels.size());
  std::vector<visio_schema_v1_service_device_info_Channel> cstructs(
      channels.size());
  for (std::size_t i = 0; i < channels.size(); ++i) {
    const Channel& c = channels[i];
    auto& cs = cstructs[i];
    cs = visio_schema_v1_service_device_info_Channel_init_zero;
    cs.id = c.id;
    cs.topic = const_cast<char*>(c.topic.c_str());
    cs.encoding = const_cast<char*>(c.encoding.c_str());
    cs.schema_name = const_cast<char*>(c.schema_name.c_str());
    cs.schema_encoding = const_cast<char*>(c.schema_encoding.c_str());
    // Leave the schema pointer null for an empty schema: nanopb encodes a
    // non-null FT_POINTER bytes field even when empty (emitting a 0-length
    // `schema` field), but proto3/libprotobuf omits an empty bytes field. Only
    // attaching the blob when non-empty keeps the announce byte-identical across
    // the two backends (see tests/golden/wire_vectors.txt).
    if (!c.schema.empty()) {
      auto& blob = schema_blobs[i];
      blob.resize(PB_BYTES_ARRAY_T_ALLOCSIZE(c.schema.size()));
      auto* arr = reinterpret_cast<pb_bytes_array_t*>(blob.data());
      arr->size = static_cast<pb_size_t>(c.schema.size());
      std::memcpy(arr->bytes, c.schema.data(), c.schema.size());
      cs.schema = arr;
    }
  }

  visio_schema_v1_service_device_info_DeviceInfo di =
      visio_schema_v1_service_device_info_DeviceInfo_init_zero;
  di.device_name = const_cast<char*>(device_name.c_str());
  di.equipment_type = OrNull(equipment_type);
  di.firmware_version = OrNull(firmware_version);
  di.hardware_revision = OrNull(hardware_revision);
  di.serial = OrNull(serial);
  di.boot_unix_seconds = boot_unix_seconds;
  di.channels_count = static_cast<pb_size_t>(cstructs.size());
  di.channels = cstructs.empty() ? nullptr : cstructs.data();

  std::size_t sz = 0;
  if (!pb_get_encoded_size(
          &sz, visio_schema_v1_service_device_info_DeviceInfo_fields, &di)) {
    throw std::logic_error("DeviceInfo: pb_get_encoded_size failed");
  }
  std::string out(sz, '\0');
  pb_ostream_t os = pb_ostream_from_buffer(
      reinterpret_cast<std::uint8_t*>(out.data()), out.size());
  if (!pb_encode(&os, visio_schema_v1_service_device_info_DeviceInfo_fields, &di)) {
    throw std::logic_error(std::string("DeviceInfo: pb_encode failed: ") +
                           PB_GET_ERROR(&os));
  }
  out.resize(os.bytes_written);
  return out;
}

bool ChannelRegistry::Decode(const std::string& payload, DeviceView* out) {
  visio_schema_v1_service_device_info_DeviceInfo di =
      visio_schema_v1_service_device_info_DeviceInfo_init_zero;
  pb_istream_t is = pb_istream_from_buffer(
      reinterpret_cast<const std::uint8_t*>(payload.data()), payload.size());
  const bool ok = pb_decode(
      &is, visio_schema_v1_service_device_info_DeviceInfo_fields, &di);
  if (ok) {
    if (di.device_name) out->device_name = di.device_name;
    if (di.equipment_type) out->equipment_type = di.equipment_type;
    if (di.firmware_version) out->firmware_version = di.firmware_version;
    if (di.hardware_revision) out->hardware_revision = di.hardware_revision;
    if (di.serial) out->serial = di.serial;
    out->boot_unix_seconds = di.boot_unix_seconds;
    out->channels.clear();
    out->channels.reserve(di.channels_count);
    for (pb_size_t i = 0; i < di.channels_count; ++i) {
      const auto& cs = di.channels[i];
      Channel c;
      c.id = cs.id;
      if (cs.topic) c.topic = cs.topic;
      if (cs.encoding) c.encoding = cs.encoding;
      if (cs.schema_name) c.schema_name = cs.schema_name;
      if (cs.schema_encoding) c.schema_encoding = cs.schema_encoding;
      if (cs.schema) {
        c.schema.assign(reinterpret_cast<const char*>(cs.schema->bytes),
                        cs.schema->size);
      }
      out->channels.push_back(std::move(c));
    }
  }
  pb_release(visio_schema_v1_service_device_info_DeviceInfo_fields, &di);
  return ok;
}

// ── Construction ────────────────────────────────────────────────────────────

ChannelRegistry::ChannelRegistry(std::string device_name,
                                 std::string firmware_version,
                                 std::string hardware_revision,
                                 std::string serial,
                                 std::uint64_t boot_unix_seconds)
    : device_name_(std::move(device_name)),
      firmware_version_(std::move(firmware_version)),
      hardware_revision_(std::move(hardware_revision)),
      serial_(std::move(serial)),
      boot_unix_seconds_(boot_unix_seconds) {
  device_info_channel_.id = kDeviceInfo;
  device_info_channel_.topic = kDeviceInfoTopic;
  device_info_channel_.encoding = kDefaultEncoding;
  device_info_channel_.schema_name = kDeviceInfoSchema;
  const std::string_view fds = visio_schema::wire::FileDescriptorSetFor(
      kDeviceInfoSchema);
  // Build-time invariant: the DeviceInfo descriptor must be in the schema-blob
  // table, else a recorder would write an undecodable /device_info. Fail loudly
  // (mirrors Python's KeyError) rather than silently recording an empty schema.
  if (fds.empty()) {
    throw std::logic_error(
        "DeviceInfo schema blob missing — regen schema_blobs (make gen)");
  }
  device_info_channel_.schema.assign(fds.data(), fds.size());
  device_info_channel_.schema_encoding = kDefaultEncoding;
}

// ── Own outputs ─────────────────────────────────────────────────────────────

std::uint32_t ChannelRegistry::Declare(const std::string& topic,
                                       const std::string& schema_name,
                                       const std::string& schema,
                                       const std::string& encoding,
                                       const std::string& schema_encoding) {
  auto it = topic_to_id_.find(topic);
  if (it != topic_to_id_.end()) return it->second;
  std::uint32_t cid = Alloc();
  Channel ch;
  ch.id = cid;
  ch.topic = topic;
  ch.encoding = encoding;
  ch.schema_name = schema_name;
  ch.schema = schema;
  ch.schema_encoding = schema_encoding;
  by_id_[cid] = std::move(ch);
  topic_to_id_[topic] = cid;
  own_ids_.insert(cid);
  return cid;
}

std::optional<std::uint32_t> ChannelRegistry::LocalIdFor(
    const std::string& topic) const {
  auto it = topic_to_id_.find(topic);
  if (it == topic_to_id_.end()) return std::nullopt;
  return it->second;
}

std::vector<Channel> ChannelRegistry::OwnChannels() const {
  std::vector<Channel> out;
  out.reserve(own_ids_.size());
  for (std::uint32_t cid : own_ids_) {
    auto it = by_id_.find(cid);
    if (it != by_id_.end()) out.push_back(it->second);
  }
  return out;
}

// ── Learned channels ──────────────────────────────────────────────────────

void ChannelRegistry::Learn(const Channel& channel) {
  auto it = topic_to_id_.find(channel.topic);
  if (it != topic_to_id_.end() && it->second != channel.id) {
    throw DuplicateTopicError("topic '" + channel.topic +
                              "' already mapped to id " +
                              std::to_string(it->second));
  }
  by_id_[channel.id] = channel;
  topic_to_id_[channel.topic] = channel.id;
}

void ChannelRegistry::Forget(const std::vector<std::uint32_t>& ids) {
  for (std::uint32_t cid : ids) {
    auto it = by_id_.find(cid);
    if (it != by_id_.end()) {
      auto tit = topic_to_id_.find(it->second.topic);
      if (tit != topic_to_id_.end() && tit->second == cid) {
        topic_to_id_.erase(tit);
      }
      by_id_.erase(it);
    }
    own_ids_.erase(cid);
  }
}

const Channel* ChannelRegistry::Resolve(std::uint32_t stream_id) const {
  if (stream_id == kDeviceInfo) return &device_info_channel_;
  auto it = by_id_.find(stream_id);
  return it == by_id_.end() ? nullptr : &it->second;
}

std::vector<Channel> ChannelRegistry::Channels() const {
  std::vector<Channel> out;
  out.reserve(by_id_.size());
  for (const auto& [cid, ch] : by_id_) out.push_back(ch);
  return out;
}

// ── Inbound ───────────────────────────────────────────────────────────────

Routed ChannelRegistry::Accept(Message msg) {
  const std::uint32_t sid = msg.stream_id;
  if (sid == kDeviceInfo) {
    OnAnnounce(msg.payload);
    return {};
  }
  if (sid < kFirstDynamic) {
    return {std::move(msg), nullptr};
  }
  const Channel* ch = Resolve(sid);
  if (ch == nullptr) {
    ++dropped_unmapped_;
    return {};
  }
  return {std::move(msg), ch};
}

// ── Discovery ───────────────────────────────────────────────────────────────

std::string ChannelRegistry::SelfInfo() const {
  // Own outputs only; learned channels propagate by the bus forwarding each
  // leaf's announce (with the ids remapped), not by recombining them here.
  return Encode(device_name_, equipment_type_, firmware_version_,
                hardware_revision_, serial_, boot_unix_seconds_, OwnChannels());
}

void ChannelRegistry::OnAnnounce(const std::string& payload) {
  DeviceView view;
  if (!Decode(payload, &view)) return;  // wire boundary: a peer may send garbage
  for (const Channel& c : view.channels) {
    try {
      Learn(c);
    } catch (const DuplicateTopicError& e) {
      std::cerr << "visio-schema: announce: " << e.what() << "\n";
    }
  }
}

}  // namespace visio_schema::routing
