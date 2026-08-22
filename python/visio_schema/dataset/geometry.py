"""The stored-frame downscale rule — one definition, two users.

An encoder sizes the video it writes with this; a live pipeline that
downscales frames before inference sizes them with the SAME call. If the two
ever disagreed, a model would serve on geometry it never trained on — a
difference of a pixel or two, invisible in every log and in every checksum
that does not compare the frames themselves.

Deliberately dependency-free (no av, no numpy) so the rule costs nothing to
import on a serving path that encodes no video.
"""

from __future__ import annotations


def scaled_dims(
    width: int, height: int, long_side: int | None
) -> tuple[int, int]:
    """Aspect-preserving ``(out_w, out_h)`` with the longer side capped at
    ``long_side``; both dims forced even (yuv420p requirement).

    ``long_side`` None (or already small enough) leaves the dims alone, bar
    the even-ing.
    """
    if long_side is None or max(width, height) <= long_side:
        ow, oh = width, height
    elif width >= height:
        ow, oh = long_side, round(height * long_side / width)
    else:
        ow, oh = round(width * long_side / height), long_side
    ow -= ow % 2
    oh -= oh % 2
    return max(2, ow), max(2, oh)
