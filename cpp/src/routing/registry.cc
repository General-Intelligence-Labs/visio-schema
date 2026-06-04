#include "visio_schema/routing/registry.hpp"

#include <pb_decode.h>
#include <pb_encode.h>

#include <cstring>
#include <iostream>
#include <utility>

#include "visio_schema/service/device_info/v1/device_info.pb.h"
#include "visio_schema/wire/v1/header.pb.h"

namespace visio_schema::routing {

namespace {

constexpr std::uint32_t kDeviceInfoId =
    visio_schema_wire_v1_ControlStream_CONTROL_STREAM_DEVICE_INFO;

// nanopb POINTER string: NULL omits the field; non-NULL (even empty) encodes it.
char* OrNull(const std::string& s) {
  return s.empty() ? nullptr : const_cast<char*>(s.c_str());
}

}  // namespace

// ── DeviceInfo encode / decode (nanopb FT_POINTER) ──────────────────────────

std::string ChannelRegistry::Encode(const std::string& device_name,
                                    const std::string& firmware_version,
                                    const std::string& hardware_revision,
                                    const std::string& serial,
                                    std::uint64_t boot_unix_seconds,
                                    const std::vector<Channel>& channels) {
  std::vector<std::vector<std::uint8_t>> schema_blobs(channels.size());
  std::vector<visio_schema_service_device_info_v1_Channel> cstructs(
      channels.size());
  for (std::size_t i = 0; i < channels.size(); ++i) {
    const Channel& c = channels[i];
    auto& cs = cstructs[i];
    cs = visio_schema_service_device_info_v1_Channel_init_zero;
    cs.id = c.id;
    cs.topic = const_cast<char*>(c.topic.c_str());
    cs.encoding = const_cast<char*>(c.encoding.c_str());
    cs.schema_name = const_cast<char*>(c.schema_name.c_str());
    cs.schema_encoding = const_cast<char*>(c.schema_encoding.c_str());
    auto& blob = schema_blobs[i];
    blob.resize(PB_BYTES_ARRAY_T_ALLOCSIZE(c.schema.size()));
    auto* arr = reinterpret_cast<pb_bytes_array_t*>(blob.data());
    arr->size = static_cast<pb_size_t>(c.schema.size());
    if (!c.schema.empty()) {
      std::memcpy(arr->bytes, c.schema.data(), c.schema.size());
    }
    cs.schema = arr;
  }

  visio_schema_service_device_info_v1_DeviceInfo di =
      visio_schema_service_device_info_v1_DeviceInfo_init_zero;
  di.device_name = const_cast<char*>(device_name.c_str());
  di.firmware_version = OrNull(firmware_version);
  di.hardware_revision = OrNull(hardware_revision);
  di.serial = OrNull(serial);
  di.boot_unix_seconds = boot_unix_seconds;
  di.channels_count = static_cast<pb_size_t>(cstructs.size());
  di.channels = cstructs.empty() ? nullptr : cstructs.data();

  std::size_t sz = 0;
  pb_get_encoded_size(&sz, visio_schema_service_device_info_v1_DeviceInfo_fields,
                      &di);
  std::string out(sz, '\0');
  pb_ostream_t os = pb_ostream_from_buffer(
      reinterpret_cast<std::uint8_t*>(out.data()), out.size());
  pb_encode(&os, visio_schema_service_device_info_v1_DeviceInfo_fields, &di);
  out.resize(os.bytes_written);
  return out;
}

bool ChannelRegistry::Decode(const std::string& payload, DeviceView* out) {
  visio_schema_service_device_info_v1_DeviceInfo di =
      visio_schema_service_device_info_v1_DeviceInfo_init_zero;
  pb_istream_t is = pb_istream_from_buffer(
      reinterpret_cast<const std::uint8_t*>(payload.data()), payload.size());
  const bool ok = pb_decode(
      &is, visio_schema_service_device_info_v1_DeviceInfo_fields, &di);
  if (ok) {
    if (di.device_name) out->device_name = di.device_name;
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
  pb_release(visio_schema_service_device_info_v1_DeviceInfo_fields, &di);
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
      boot_unix_seconds_(boot_unix_seconds) {}

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
  if (sid == kDeviceInfoId) {
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
  return Encode(device_name_, firmware_version_, hardware_revision_, serial_,
                boot_unix_seconds_, Channels());
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
