"""Signatures that print clearly.

A signature is drawn on a 960 x 300 pad with a 2.6 px pen and printed in a box about
34 mm wide. Shrunk that far the line is a hairline — under 0.1 mm — and the empty
pad around the mark shrinks it further. That is why signed sheets look pale.

This makes a print copy of a stored signature: cut to the ink, the line brought up to
a pen-like width for the size it will print at, solid dark ink. The stored file is
never changed — the copy sits beside it in a cache and is rebuilt if the original
changes — so every sheet ever signed prints clearly, old ones included.
"""
import os

INK = (11, 26, 58)          # dark blue-black: reads as pen on paper, copies as black
PEN_MM = 0.42               # line width on paper
BOX_MM = (34.0, 13.0)       # the signature box most sheets print into


def _stroke_px(mask):
    """Average line width of a mask: area / half its edge (w = 2A / P for thin lines)."""
    from PIL import ImageFilter, ImageChops
    er = mask.filter(ImageFilter.MinFilter(3))
    edge = ImageChops.subtract(mask, er)
    a = sum(1 for v in mask.getdata() if v)
    p = sum(1 for v in edge.getdata() if v)
    return (2.0 * a / p) if p else 0.0


def clear_sig(src, box_mm=BOX_MM):
    """Path of a print-ready copy of the signature at `src`, or `src` if anything fails."""
    try:
        if not src or not os.path.exists(src):
            return src
        d = os.path.join(os.path.dirname(src), ".sigprint")
        os.makedirs(d, exist_ok=True)
        st = os.stat(src)
        out = os.path.join(d, f"{os.path.splitext(os.path.basename(src))[0]}_{int(st.st_mtime)}_{st.st_size}.png")
        if os.path.exists(out):
            return out
        from PIL import Image, ImageFilter
        with Image.open(src) as im:
            im.load()
            if im.mode in ("RGBA", "LA") or "transparency" in im.info:
                a = im.convert("RGBA").getchannel("A")
            else:                                   # a flat image: ink is what is dark
                a = im.convert("L").point(lambda v: 255 - v)
        a = a.point(lambda v: 255 if v > 40 else 0)
        bb = a.getbbox()
        if not bb:
            return src
        m = max(6, int(0.04 * max(bb[2] - bb[0], bb[3] - bb[1])))
        bb = (max(0, bb[0] - m), max(0, bb[1] - m), min(a.width, bb[2] + m), min(a.height, bb[3] + m))
        a = a.crop(bb)
        w, h = a.size
        # how wide the mark will be on paper, then how many pixels a pen line needs
        mm_per_px = min(box_mm[0] / w, box_mm[1] / h)
        want = PEN_MM / mm_per_px
        have = _stroke_px(a) or 2.0
        grow = int(round(want - have))
        if grow >= 2:
            k = grow + 1 if grow % 2 == 0 else grow
            a = a.filter(ImageFilter.MaxFilter(min(k, 15)))
        a = a.filter(ImageFilter.GaussianBlur(0.6)).point(lambda v: min(255, v * 2))
        ink = Image.new("RGBA", a.size, INK + (0,))
        ink.putalpha(a)
        ink.save(out, "PNG", optimize=True)
        return out
    except Exception:
        return src
