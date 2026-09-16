"""The multi-board OTA package.

Why it exists: visio_schema/wire/package.py. These pin the three properties the
format was chosen FOR -- index first, self last, and an authenticated roster --
because each is a rule a "simplification" would quietly drop.
"""
import io
import tarfile

import pytest

from visio_schema.wire import ota, package

KEY = bytes(range(32))
HAND = {"hwrev": "compact_umi", "equipment": "gripper", "soc": "rv1106",
        "blob": b"VENC" + bytes(60)}
HEAD = {"hwrev": "ego_pro_head", "equipment": "ego", "soc": "rv1126b",
        "blob": b"VENC" + bytes(100)}


_DEFAULT = object()


def build(images=_DEFAULT, **kw):
    kw.setdefault("product", "ego_pro")
    kw.setdefault("version", "1.3.0")
    kw.setdefault("key", KEY)
    kw.setdefault("self_hwrev", "ego_pro_head")
    if images is _DEFAULT:
        images = [HAND, HEAD]
    return package.build(images, **kw)


def names(pkg):
    return tarfile.open(fileobj=io.BytesIO(pkg), mode="r:").getnames()


def test_the_index_is_member_zero():
    """Tar has no table of contents, so a reader discovers members as it goes.
    The receiving device must validate the WHOLE roster before it erases
    anything, and reading member 0 is what buys that back."""
    assert names(build())[0] == package.INDEX_NAME


def test_the_receiving_devices_own_image_is_last():
    """By the time it arrives, every refusal the index can produce has fired.
    What is left is a write failure -- and finding one BEFORE the inactive slot
    is erased is strictly better than after."""
    assert names(build())[-1] == "ego_pro_head.img"
    # And it follows self_hwrev, not the input order.
    assert names(build([HEAD, HAND]))[-1] == "ego_pro_head.img"
    assert names(build(self_hwrev="compact_umi"))[-1] == "compact_umi.img"


def test_it_roundtrips_with_a_digest_and_size_per_board():
    idx = package.read_index(build(), KEY)
    assert idx["product"] == "ego_pro" and idx["version"] == "1.3.0"
    by = {b["hwrev"]: b for b in idx["boards"]}
    assert by["compact_umi"]["bytes"] == len(HAND["blob"])
    assert by["ego_pro_head"]["file"] == "ego_pro_head.img"
    # The per-board digest lets a head verify a slice before relaying it, with
    # no side manifest.
    import hashlib
    assert by["compact_umi"]["sha256"] == hashlib.sha256(HAND["blob"]).hexdigest()


def test_a_relabelled_board_fails_the_hmac():
    """THE attack the HMAC exists for. Every image is independently encrypted
    and self-digesting, so one cannot be forged -- but swapping which BOARD an
    image claims to be for hands a genuine head image to a limb whose own board
    mark then passes. An RV1126B image on an RV1106 is maskrom-only recovery."""
    pkg = build()
    tf = tarfile.open(fileobj=io.BytesIO(pkg), mode="r:")
    text = tf.extractfile(package.INDEX_NAME).read().decode()
    # Relabel the hand's image as the head's, leaving the hmac line untouched.
    tampered = text.replace("board=compact_umi:compact_umi:",
                            "board=compact_umi:ego_pro_head:")
    assert tampered != text
    with pytest.raises(package.PackageError, match="HMAC"):
        package.parse_index(tampered, KEY)
    # ...and it parses fine without the key, so the refusal is the HMAC and not
    # some accident of the grammar.
    assert package.parse_index(tampered)["boards"][0]["hwrev"] == "ego_pro_head"


def test_an_unknown_index_version_is_refused_rather_than_guessed():
    text = tarfile.open(fileobj=io.BytesIO(build()), mode="r:").extractfile(
        package.INDEX_NAME).read().decode()
    with pytest.raises(package.PackageError, match="refusing to guess"):
        package.parse_index(text.replace("v=1", "v=2"))


def test_an_index_that_is_not_member_zero_is_refused():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for name, data in (("compact_umi.img", b"x"), (package.INDEX_NAME, b"v=1\n")):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    with pytest.raises(package.PackageError, match="member 0"):
        package.read_index(buf.getvalue(), KEY)


def test_the_same_inputs_make_the_same_bytes():
    """A release artifact that differs run to run cannot be checked against a
    published digest."""
    assert build() == build()


def test_two_images_for_one_board_are_refused():
    with pytest.raises(package.PackageError, match="two images"):
        build([HAND, dict(HAND)])


def test_an_empty_package_is_refused():
    with pytest.raises(package.PackageError):
        build([])


def test_a_bare_venc_is_not_a_package():
    """The device dispatches on this: VENC is the single image every fielded
    ego has always parsed, and it must keep reading as exactly that."""
    assert not package.is_package(b"VENC" + bytes(600))
    assert package.is_package(build())


def test_it_can_be_listed_without_the_key():
    """A laptop inspecting a release has no bundle key; refusing to LIST would
    make the artifact opaque to the person holding it."""
    idx = package.read_index(build())
    assert [b["hwrev"] for b in idx["boards"]] == ["compact_umi", "ego_pro_head"]


def test_bundle_error_accepts_a_package_and_still_rejects_a_raw_image():
    assert ota.bundle_error(build()) is None
    assert ota.bundle_error(b"VENC" + bytes(60)) is None
    assert "RKFW" in (ota.bundle_error(b"RKFW" + bytes(600)) or "")
