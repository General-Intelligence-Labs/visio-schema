/*
 * golden_vectors_test_util.hpp - load a tests/golden/*.txt vector file.
 *
 * Extracted so the suites that read one stop each carrying their own hex parser.
 * Two suites reading one fixture with two ad-hoc parsers is the same drift these
 * fixtures exist to prevent, one level up -- the reasoning
 * visio-embedded/tests/unit/config/seal_golden.hpp already states for the
 * cross-repo copy.
 *
 * Grammar: `#` comments, blank lines, and `key=lowercase_hexbytes`.
 */
#ifndef VISIO_SCHEMA_TESTS_GOLDEN_VECTORS_TEST_UTIL_HPP
#define VISIO_SCHEMA_TESTS_GOLDEN_VECTORS_TEST_UTIL_HPP

#include <fstream>
#include <map>
#include <string>

#ifndef VISIO_GOLDEN_DIR
#error "VISIO_GOLDEN_DIR must be defined by the build (path to tests/golden)"
#endif

namespace visio_golden {

inline std::string FromHex(const std::string& h) {
  std::string out;
  out.reserve(h.size() / 2);
  for (std::size_t i = 0; i + 1 < h.size(); i += 2)
    out.push_back(static_cast<char>(std::stoi(h.substr(i, 2), nullptr, 16)));
  return out;
}

inline std::string Hex(const std::string& s) {
  static const char* d = "0123456789abcdef";
  std::string out;
  out.reserve(s.size() * 2);
  for (unsigned char c : s) {
    out.push_back(d[c >> 4]);
    out.push_back(d[c & 0xF]);
  }
  return out;
}

inline std::map<std::string, std::string> Load(const std::string& name) {
  std::map<std::string, std::string> out;
  std::ifstream f(std::string(VISIO_GOLDEN_DIR) + "/" + name);
  std::string line;
  while (std::getline(f, line)) {
    if (line.empty() || line[0] == '#') continue;
    const std::size_t eq = line.find('=');
    if (eq == std::string::npos) continue;
    out[line.substr(0, eq)] = FromHex(line.substr(eq + 1));
  }
  return out;
}

}  // namespace visio_golden

#endif  // VISIO_SCHEMA_TESTS_GOLDEN_VECTORS_TEST_UTIL_HPP
