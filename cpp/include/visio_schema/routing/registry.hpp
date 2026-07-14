// ChannelRegistry — the single-source topic/schema table for one peer. Mirrors
// python/visio_schema/routing.py.
//
// Maps stream_id -> Channel for ONE source: a schema-only consumer reads one
// link (the device's announce + data share one id space); on a bus the bus
// remaps each endpoint's local ids into its global space before the registry
// sees them. So there is no source_key — just own outputs (declared locally) +
// learned channels (from announces, already in this peer's id space) in one
// id -> Channel map, with the invariant that a topic maps to exactly one id. The
// bus calls Forget() when a link drops so the topic frees for a reconnect.
#pragma once

#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "visio_schema/routing/channel.hpp"   // Channel, Routed, DuplicateTopicError
#include "visio_schema/wire/control.hpp"       // kFirstDynamic
#include "visio_schema/wire/message.hpp"

namespace visio_schema::routing {

using visio_schema::wire::Message;

class ChannelRegistry {
 public:
  struct DeviceView {
    std::string device_name;
    std::string equipment_type;
    std::string firmware_version;
    std::string hardware_revision;
    std::string serial;
    std::uint64_t boot_unix_seconds = 0;
    std::vector<Channel> channels;
  };

  explicit ChannelRegistry(std::string device_name = "",
                           std::string firmware_version = "",
                           std::string hardware_revision = "",
                           std::string serial = "",
                           std::uint64_t boot_unix_seconds = 0);

  // Allocate the next stream id (monotonic, never reused). Used by Declare for
  // own outputs AND by the bus for learned-channel globals — one counter, so the
  // two id spaces never overlap.
  std::uint32_t Alloc() { return next_id_++; }

  // ── Own outputs ──────────────────────────────────────────────────────
  std::uint32_t Declare(const std::string& topic, const std::string& schema_name,
                        const std::string& schema = "",
                        const std::string& encoding = kDefaultEncoding,
                        const std::string& schema_encoding = kDefaultEncoding);
  std::optional<std::uint32_t> LocalIdFor(const std::string& topic) const;
  std::vector<Channel> OwnChannels() const;

  // ── Learned channels (already in this peer's id space) ───────────────
  void Learn(const Channel& channel);              // throws DuplicateTopicError
  void Forget(const std::vector<std::uint32_t>& ids);

  // ── Resolution ───────────────────────────────────────────────────────
  const Channel* Resolve(std::uint32_t stream_id) const;
  std::vector<Channel> Channels() const;
  // True iff this peer has declared outputs to announce (the announce is
  // own-only; learned channels propagate by the bus forwarding leaf announces).
  bool HasOwnOutputs() const { return !own_ids_.empty(); }
  std::uint64_t dropped_unmapped() const { return dropped_unmapped_; }

  // ── Inbound (no-bus single-source consumer) ──────────────────────────
  Routed Accept(Message msg);

  // ── Discovery ─────────────────────────────────────────────────────────
  // Set the optional device metadata carried in the announce (the Bus creates
  // the registry with just a device_name; a producer sets the rest here).
  // `equipment_type` is the logical role (e.g. "glove_left") — distinct from the
  // per-unit `device_name` set at construction.
  void SetMetadata(std::string equipment_type, std::string firmware_version,
                   std::string hardware_revision, std::string serial,
                   std::uint64_t boot_unix_seconds) {
    equipment_type_ = std::move(equipment_type);
    firmware_version_ = std::move(firmware_version);
    hardware_revision_ = std::move(hardware_revision);
    serial_ = std::move(serial);
    boot_unix_seconds_ = boot_unix_seconds;
  }
  std::string SelfInfo() const;          // serialized DeviceInfo announce
  void OnAnnounce(const std::string& payload);

  // DeviceInfo announce envelope (nanopb FT_POINTER). Public so the bus + tests
  // build/inspect announces.
  static std::string Encode(const std::string& device_name,
                            const std::string& equipment_type,
                            const std::string& firmware_version,
                            const std::string& hardware_revision,
                            const std::string& serial,
                            std::uint64_t boot_unix_seconds,
                            const std::vector<Channel>& channels);
  static bool Decode(const std::string& payload, DeviceView* out);

 private:
  std::string device_name_;
  std::string equipment_type_;
  std::string firmware_version_;
  std::string hardware_revision_;
  std::string serial_;
  std::uint64_t boot_unix_seconds_;

  std::unordered_map<std::uint32_t, Channel> by_id_;      // id -> Channel (own + learned)
  std::unordered_map<std::string, std::uint32_t> topic_to_id_;  // topic -> id (unique)
  std::unordered_set<std::uint32_t> own_ids_;             // ids that are our outputs
  // Resolution-only well-known channel for the DeviceInfo control stream, so a
  // recorder can write forwarded announces on one "/device_info" topic. Kept out
  // of by_id_ so it never appears in Channels()/own outputs/announces.
  Channel device_info_channel_;
  std::uint32_t next_id_ = kFirstDynamic;
  std::uint64_t dropped_unmapped_ = 0;
};

}  // namespace visio_schema::routing
