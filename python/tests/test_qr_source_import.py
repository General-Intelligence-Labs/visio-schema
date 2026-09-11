"""QR-only consumers can import the package before protobuf code generation."""
import subprocess
import sys


def test_qr_import_does_not_load_generated_bindings():
    subprocess.run([sys.executable, "-c", """
import sys
class RefuseGenerated:
    def find_spec(self, fullname, *args):
        if fullname.startswith(('visio_schema.v1', 'visio_schema.foxglove')):
            raise AssertionError('QR import needs generated bindings: ' + fullname)
sys.meta_path.insert(0, RefuseGenerated())
from visio_schema.crypto import fleet_public_key, generate_keypair
from visio_schema.settings_qr import SealedSecrets, seal_into, open_sealed
private, public = generate_keypair()
key = bytes(range(32))
payload = seal_into({'meta': {'capturer': 'test'}}, SealedSecrets(recording_key=key), pubkey=public)
assert open_sealed(payload, private).recording_key == key
assert len(fleet_public_key()) == 32
"""], check=True)


def test_native_vendoring_skips_missing_generated_sources(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "vendor_native", Path(__file__).parents[1] / "_vendor_native.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cpp = tmp_path / "cpp"
    cpp.mkdir()
    nanopb = tmp_path / "nanopb"
    nanopb.mkdir()
    (nanopb / "pb_decode.c").touch()
    monkeypatch.setattr(module, "CPP", str(cpp))
    monkeypatch.setattr(module, "NANOPB", str(nanopb))
    monkeypatch.setattr(module, "VENDOR", str(tmp_path / "vendor"))
    assert module.vendor() is False
    assert not (tmp_path / "vendor").exists()
