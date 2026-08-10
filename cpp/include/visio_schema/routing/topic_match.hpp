// Glob matching over a Visio topic's path segments — the pattern language a
// per-connection stream policy is written in (SetStreamPolicy, command.proto).
//
// Grammar:
//   *    exactly one segment
//   **   zero or more segments, at any position
//   anything else is a literal segment
//
// A leading '/' is stripped from both sides, so "/ego/camera/0" and
// "ego/camera/0" are the same topic.
//
// `**` works in a LEADING position, not just a trailing one, because a client
// does not always know a topic's depth. Relayed leaf topics normally arrive
// unchanged, but a relay MAY namespace them under the leaf's device_name
// (`Bus::RemapAnnounce` with device-name prefixing — off by default,
// opted into for multi-device bring-up). `**/camera/0` matches either form.
//
// "cam*" is NOT a prefix glob; it is a literal segment that only equals the text
// "cam*". Partial matching would make rule order much harder to reason about for
// no case we have.
#pragma once

#include <array>
#include <cstddef>
#include <string_view>

namespace visio_schema::routing {
namespace detail {

// A Visio topic is /<device>/<group>/<index>/<sub-field>; the deepest real case
// is a hub-prefixed leaf sub-field (5). The cap only bounds the split buffer.
inline constexpr std::size_t kMaxSegments = 12;

// Split on '/' into `out`, dropping a leading '/'. Returns the count, or
// kMaxSegments + 1 if it does not fit (treated as no-match by the caller).
inline std::size_t Split(std::string_view s,
                         std::array<std::string_view, kMaxSegments>& out) {
  if (!s.empty() && s.front() == '/') s.remove_prefix(1);
  std::size_t n = 0;
  while (true) {
    if (n >= kMaxSegments) return kMaxSegments + 1;
    const std::size_t slash = s.find('/');
    if (slash == std::string_view::npos) {
      out[n++] = s;
      return n;
    }
    out[n++] = s.substr(0, slash);
    s.remove_prefix(slash + 1);
  }
}

}  // namespace detail

// True iff `topic` matches `pattern` under the grammar above. An empty pattern
// or topic matches nothing — a rule that silently swallowed everything would be
// the worst possible failure mode for a filter.
inline bool TopicMatches(std::string_view pattern, std::string_view topic) {
  std::array<std::string_view, detail::kMaxSegments> p, t;
  const std::size_t np = detail::Split(pattern, p);
  const std::size_t nt = detail::Split(topic, t);
  if (np > detail::kMaxSegments || nt > detail::kMaxSegments) return false;
  if (np == 0 || nt == 0) return false;
  if (p[0].empty() || t[0].empty()) return false;  // "" or "/"

  // Greedy scan with one backtrack point — the standard wildcard match, over
  // segments instead of characters. `star` remembers the last '**' so a failed
  // literal run can retry with '**' having swallowed one more segment.
  std::size_t i = 0, j = 0, star = np, star_j = 0;
  while (j < nt) {
    if (i < np && p[i] == "**") {
      star = i++;
      star_j = j;
    } else if (i < np && (p[i] == "*" || p[i] == t[j])) {
      ++i;
      ++j;
    } else if (star != np) {
      i = star + 1;
      j = ++star_j;
    } else {
      return false;
    }
  }
  // Pattern must be exhausted, modulo trailing '**' matching zero segments —
  // otherwise "*/camera" would match "/ego/camera/0" and a rule meant for the
  // video stream would also catch every sub-field under it.
  while (i < np && p[i] == "**") ++i;
  return i == np;
}

}  // namespace visio_schema::routing
