// creader.cc — native CDC-ACM frame reader for visio_schema (the `_creader` ext).
//
// Binds the existing C++ SerialEndpoint + a MessageInbox: the endpoint's reader
// thread deframes with the GIL released and Push()es into the inbox; poll_batch()
// pops batches on Python's own schedule. So a CPU-bound consumer can stall the
// dispatch without ever stalling the read — the inbox absorbs the backlog instead
// of the kernel buffer overflowing. Each Frame owns its Message and exposes the
// payload zero-copy via the buffer protocol: memoryview(frame) pins the Frame, so
// the bytes outlive the batch (the MCAP writer thread holds the memoryview across
// an async write).
#include <pybind11/pybind11.h>

#include <cstddef>
#include <cstdint>
#include <string>
#include <utility>
#include <vector>

#include "visio_schema/transport/framing.hpp"   // ExtractFrames (deframe helper)
#include "visio_schema/transport/message_inbox.hpp"
#include "visio_schema/transport/serial.hpp"
#include "visio_schema/wire/message.hpp"
#include "visio_schema/wire/time.hpp"            // TimestampNs / SetTimestampNs

namespace py = pybind11;
using visio_schema::transport::Endpoint;
using visio_schema::transport::MessageInbox;
using visio_schema::transport::SerialEndpoint;
using visio_schema::transport::WritePolicy;
using visio_schema::wire::Message;

namespace {

// One decoded frame, owning its Message. ts_ns is a plain int (no protobuf
// Timestamp built here); the payload is the zero-copy buffer.
struct Frame {
  Message msg;
  std::uint32_t stream_id() const { return msg.stream_id; }
  std::uint32_t seq() const { return msg.seq; }
  std::int64_t ts_ns() const { return visio_schema::TimestampNs(msg.timestamp); }
};

class Reader {
 public:
  Reader(std::string path, std::size_t max_depth)
      : inbox_(WritePolicy::drop_oldest(max_depth)),
        endpoint_(std::move(path), WritePolicy::drop_oldest(max_depth)) {}

  ~Reader() { stop(); }

  void start() {
    if (started_) return;
    started_ = true;
    // Reopenable serial self-heals; on_closed never fires (the inbox is closed
    // only by stop()).
    endpoint_.Start([this](Message m, Endpoint*) { inbox_.Push(std::move(m)); },
                    [](Endpoint*) {});
  }

  void stop() {
    if (!started_) return;
    {
      py::gil_scoped_release release;  // Stop() joins the reader thread
      endpoint_.Stop();
    }
    inbox_.Close();
    started_ = false;
  }

  // Block (GIL released) up to timeout_ms for frames; return up to max_frames
  // (0 = no cap) as a list[Frame]. One GIL acquisition amortizes the whole batch.
  py::list poll_batch(int timeout_ms, std::size_t max_frames) {
    std::vector<Message> msgs;
    {
      py::gil_scoped_release release;
      msgs = inbox_.PopBatch(timeout_ms, max_frames);
    }
    py::list out(msgs.size());
    for (std::size_t i = 0; i < msgs.size(); ++i)
      out[i] = Frame{std::move(msgs[i])};
    return out;
  }

  // Enqueue one already-stamped outbound message (low-rate control). The endpoint
  // frames + writes it via its thread-safe outbox; payload stays opaque bytes.
  void send(std::uint32_t stream_id, std::uint32_t seq, std::int64_t ts_ns,
            const std::string& payload) {
    Message m;
    m.stream_id = stream_id;
    m.seq = seq;
    visio_schema::SetTimestampNs(&m.timestamp, ts_ns);
    m.payload = payload;
    endpoint_.Send(m);
  }

  std::uint64_t dropped() const { return inbox_.Dropped(); }

 private:
  MessageInbox inbox_;
  SerialEndpoint endpoint_;
  bool started_ = false;
};

}  // namespace

PYBIND11_MODULE(_creader, m) {
  m.doc() = "Native GIL-free CDC-ACM frame reader for visio_schema.";

  py::class_<Frame>(m, "Frame", py::buffer_protocol())
      .def_buffer([](Frame& f) -> py::buffer_info {
        return py::buffer_info(
            const_cast<char*>(f.msg.payload.data()), 1,
            py::format_descriptor<unsigned char>::format(), 1,
            {static_cast<py::ssize_t>(f.msg.payload.size())}, {1},
            /*readonly=*/true);
      })
      .def_property_readonly("stream_id", &Frame::stream_id)
      .def_property_readonly("seq", &Frame::seq)
      .def_property_readonly("ts_ns", &Frame::ts_ns)
      .def_property_readonly("payload", [](py::object self) {
        // memoryview holds a ref to `self` (the Frame), so the bytes stay valid
        // for as long as the memoryview (or anything holding it) does.
        return py::reinterpret_steal<py::object>(
            PyMemoryView_FromObject(self.ptr()));
      });

  py::class_<Reader>(m, "Reader")
      .def(py::init<std::string, std::size_t>(), py::arg("path"),
           py::arg("max_depth") = 4096)
      .def("start", &Reader::start)
      .def("stop", &Reader::stop)
      .def("poll_batch", &Reader::poll_batch, py::arg("timeout_ms"),
           py::arg("max_frames") = 0)
      .def("send", &Reader::send, py::arg("stream_id"), py::arg("seq"),
           py::arg("ts_ns"), py::arg("payload"))
      .def("dropped", &Reader::dropped);

  // Synchronous deframe over a COBS-delimited buffer — the SAME C++ ExtractFrames
  // the reader thread runs, exposed for deterministic parity tests against the
  // pure-Python extract_frames. Returns (list[Frame], bytes_consumed); a trailing
  // partial frame is left unconsumed, matching the pure path.
  m.def(
      "deframe",
      [](const std::string& data) {
        std::vector<std::uint8_t> buf(data.begin(), data.end());
        const std::size_t before = buf.size();
        std::vector<Message> msgs;
        {
          py::gil_scoped_release release;
          msgs = visio_schema::transport::ExtractFrames(buf);
        }
        py::list frames(msgs.size());
        for (std::size_t i = 0; i < msgs.size(); ++i)
          frames[i] = Frame{std::move(msgs[i])};
        return py::make_tuple(frames, before - buf.size());
      },
      py::arg("data"));
}
