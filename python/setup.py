"""Build the optional native `visio_schema._creader` extension.

Metadata lives in pyproject.toml; this file only declares the C/C++ extension.
The ext is `optional=True`, so a missing compiler / build failure degrades to the
pure-Python reader instead of failing `pip install` (matters for fsglove's
editable install on a toolchain-less box). It is built only on linux/macOS; the
pure-Python path covers everything else (and Windows, which is out of scope).

The sources are the SAME C++ codec + transport the firmware/C++ tests compile, so
the native reader is byte-identical to the wire by construction. setuptools
requires Extension sources to live UNDER this directory, so `_vendor_native.py`
copies the sibling cpp/ + nanopb trees into `_native_build/` first (a no-op in the
cibuildwheel container, where CI has pre-vendored them on the host).
"""
import os
import sys

from setuptools import Extension, setup

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "_native_build")


def _ext_modules():
    if sys.platform not in ("linux", "darwin"):  # Windows uses the pure-Python path
        return []
    import pybind11  # build-time only; in [build-system].requires

    import _vendor_native

    _vendor_native.vendor()  # refresh _native_build/ from siblings when present
    if not os.path.isdir(VENDOR):
        # Neither siblings nor a pre-vendored tree — skip the ext (optional);
        # install still succeeds and falls back to pure-Python.
        return []

    def v(*parts: str) -> str:
        return os.path.relpath(os.path.join(VENDOR, *parts), HERE)

    sources = [
        os.path.join("src", "creader.cc"),
        # reused C++ wire codec + transport (byte-identical to firmware)
        v("src", "codec", "crc16.cc"),
        v("src", "codec", "cobs.cc"),
        v("src", "codec", "frame.cc"),
        v("src", "transport", "framing.cc"),
        v("src", "transport", "link.cc"),
        v("src", "transport", "framed_outbox.cc"),
        v("src", "transport", "framed_fd.cc"),
        v("src", "transport", "serial.cc"),
        # nanopb runtime + the wire Header (+ its embedded Timestamp)
        v("nanopb", "pb_decode.c"),
        v("nanopb", "pb_encode.c"),
        v("nanopb", "pb_common.c"),
        v("generated_nanopb", "visio_schema", "v1", "wire", "header.pb.c"),
        v("generated_nanopb", "google", "protobuf", "timestamp.pb.c"),
    ]
    return [
        Extension(
            "visio_schema._creader",
            sources=sources,
            include_dirs=[os.path.join(VENDOR, "include"),
                          os.path.join(VENDOR, "generated_nanopb"),
                          os.path.join(VENDOR, "nanopb"),
                          pybind11.get_include()],
            define_macros=[("PB_ENABLE_MALLOC", "1")],
            extra_compile_args=["-std=c++17"],  # ignored-with-warning on the .c files
            language="c++",
            optional=True,
        )
    ]


setup(ext_modules=_ext_modules())
