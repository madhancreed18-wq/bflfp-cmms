"""Normalize the three login logos so BFL / BFLFP / BFLPC render the same size.

fac_fp.png has a border frame + tight crop baked into the image, so it looks
bigger than the others. This trims that frame, trims surrounding background,
and pads all three onto a uniform transparent square canvas.

Run once from the project folder:   python fix_logos.py
Then hard-refresh the app. Originals are saved as *.png.bak.
"""
import shutil
from PIL import Image, ImageChops

# (filename, fraction to crop off each edge first to drop a baked-in frame)
LOGOS = [("logo.png", 0.0), ("fac_fp.png", 0.06), ("fac_pc.png", 0.0)]


def trim(im):
    bg = Image.new("RGBA", im.size, im.getpixel((0, 0)))
    bb = ImageChops.difference(im, bg).getbbox()
    return im.crop(bb) if bb else im


for name, frame in LOGOS:
    p = "static/" + name
    shutil.copy(p, p + ".bak")
    im = Image.open(p).convert("RGBA")
    if frame:
        w, h = im.size
        c = int(min(w, h) * frame)
        im = im.crop((c, c, w - c, h - c))
    im = trim(im)
    w, h = im.size
    s = max(w, h)
    pad = int(s * 0.12)
    cv = s + 2 * pad
    out = Image.new("RGBA", (cv, cv), (255, 255, 255, 0))
    out.paste(im, ((cv - w) // 2, (cv - h) // 2), im)
    out.save(p)
    print(f"{name}: -> {out.size}  (backup: {name}.bak)")

print("Done. Hard-refresh the app to see it.")
