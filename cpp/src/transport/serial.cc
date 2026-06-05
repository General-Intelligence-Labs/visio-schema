#include "visio_schema/transport/serial.hpp"

#include <dirent.h>
#include <cstdio>

namespace visio_schema::transport {

SerialEndpoint::SerialEndpoint(FdFactory factory, WritePolicy policy,
                               UsbStateFn usb_state)
    : FramedFdEndpoint(std::move(factory), policy),
      watchdog_enabled_(true),
      usb_state_fn_(std::move(usb_state)) {}

SerialEndpoint::SerialEndpoint(std::string path, WritePolicy policy,
                               UsbStateFn usb_state)
    : SerialEndpoint(MakeFactory(std::move(path)), policy, std::move(usb_state)) {}

// Runs on the endpoint's own I/O thread (FramedFdEndpoint::Loop -> Tick).
void SerialEndpoint::Tick(std::int64_t now_ns) {
  if (!watchdog_enabled_) {
    FramedFdEndpoint::Tick(now_ns);  // plain reopen-with-backoff
    return;
  }
  const std::string usb = usb_state_fn_ ? usb_state_fn_() : std::string();
  const auto action = watchdog_.tick(usb, outbox_pending(), link_up_unlocked(),
                                     now_ns / 1'000'000);
  if (action == SerialWatchdog::Action::None) return;
  // CONFIGURED edge / drain-stall / retry: drop the (possibly stale) link + outbox
  // and open a fresh one. The blocking close/open is on THIS leg's thread only.
  MarkLinkDead();
  Reopen();
  watchdog_.on_reopen_result(link_up_unlocked());
}

// Mirrors umi_embedded/src/collector.cpp::read_usb_state.
std::string SerialEndpoint::ReadUsbState() {
  auto read_trim_upper = [](const char* path) -> std::string {
    FILE* f = std::fopen(path, "r");
    if (!f) return {};
    char buf[64] = {};
    std::size_t n = std::fread(buf, 1, sizeof(buf) - 1, f);
    std::fclose(f);
    while (n > 0 && (buf[n - 1] == '\n' || buf[n - 1] == ' ')) buf[--n] = 0;
    for (std::size_t i = 0; i < n; i++)
      if (buf[i] >= 'a' && buf[i] <= 'z') buf[i] = buf[i] - 'a' + 'A';
    return std::string(buf, n);
  };
  std::string s = read_trim_upper("/sys/class/android_usb/android0/state");
  if (!s.empty()) return s;
  DIR* d = opendir("/sys/class/udc");
  if (!d) return {};
  std::string out;
  for (struct dirent* e; (e = readdir(d)) != nullptr;) {
    if (e->d_name[0] == '.') continue;
    char path[300];
    std::snprintf(path, sizeof(path), "/sys/class/udc/%s/state", e->d_name);
    out = read_trim_upper(path);
    if (!out.empty()) break;
  }
  closedir(d);
  return out;
}

}  // namespace visio_schema::transport
