# ══════════════════════════════════════════════════════════════════════════════════
#  Job photos — where a picture came from, and saying so on the picture
#
#  A photo on a repair form is evidence. "Before" is meant to show the machine as it
#  was found and "after" as it was left, and the whole value of the pair is that they
#  were taken at those two moments. Nothing in the app used to record whether a photo
#  was taken just now or picked out of the phone's gallery from last month, and nothing
#  on the printed sheet said either.
#
#  Three signals, strongest first, because no single one of them is proof:
#
#    src="camera"   the technician used the camera INSIDE the app: a live preview and a
#                   shutter, the frame drawn by the page itself. The file never came
#                   from storage, so it cannot be an old picture. This is the only
#                   actual proof, and it is what the photo buttons now use.
#    src="capture"  the phone's camera app, opened directly by capture="environment".
#                   A strong nudge — some browsers still offer the gallery behind it.
#    src="upload"   picked from storage. Accepted, because sometimes the only photo of
#                   a failed part was taken before anybody raised a job — but LABELLED,
#                   on screen and on the form.
#
#  EXIF is evidence, not proof: a real camera file carries DateTimeOriginal and the
#  phone model; a screenshot or an edited file carries nothing. The browser reads it
#  from the original file (the canvas copy it uploads has none) and sends it along, so
#  it is a claim by the device rather than something this server measured — useful for
#  "this picture was taken three days before the job was reported", not for an argument.
#
#  The stamp is drawn HERE, on the server, for one reason: a stamp drawn in the browser
#  can be faked by uploading an already-stamped file. The time on it is the server's.
# ══════════════════════════════════════════════════════════════════════════════════
import logging
import os
from datetime import datetime

from .config import STATIC

_log = logging.getLogger("cmms")

SRC_LABEL = {
    "camera": ("ถ่ายในแอป", "in-app camera"),
    "capture": ("กล้องมือถือ", "phone camera"),
    "upload": ("อัปโหลดจากเครื่อง", "uploaded from device"),
}


def _font(px, bold=False):
    """The Thai-capable face from static/fonts, else whatever PIL can find.

    Regular or bold, never italic — an italic face was being picked purely because its
    filename sorted first, and the caption on a photo is not the place for it.
    """
    from PIL import ImageFont
    d = os.path.join(STATIC, "fonts")
    try:
        names = [n for n in sorted(os.listdir(d))
                 if n.lower().endswith(".ttf") and "italic" not in n.lower()]
        want = [n for n in names if ("bold" in n.lower()) == bool(bold)] or names
        for n in want:
            try:
                return ImageFont.truetype(os.path.join(d, n), px)
            except OSError:
                continue
    except OSError:
        pass
    for n in ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
              "C:/Windows/Fonts/tahomabd.ttf" if bold else "C:/Windows/Fonts/tahoma.ttf"):
        try:
            return ImageFont.truetype(n, px)
        except OSError:
            continue
    return ImageFont.load_default()


def read_exif(raw):
    """{dt, make, model} from the ORIGINAL bytes, or {} — never raises."""
    out = {}
    try:
        import io
        from PIL import Image, ExifTags
        img = Image.open(io.BytesIO(raw))
        ex = img.getexif()
        if not ex:
            return {}
        tags = {ExifTags.TAGS.get(k, k): v for k, v in ex.items()}
        dt = tags.get("DateTimeOriginal") or tags.get("DateTime")
        if dt:
            out["dt"] = str(dt).replace(":", "-", 2)      # 2026:09:12 → 2026-09-12
        for k in ("Make", "Model"):
            if tags.get(k):
                out[k.lower()] = str(tags[k])[:40]
    except Exception:
        return {}
    return out


def _wrap(dr, text, font, limit):
    """`text` split into lines that each fit inside `limit` pixels."""
    words, out, cur = str(text).split(" "), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if cur and dr.textlength(trial, font=font) > limit:
            out.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        out.append(cur)
    return out[:3]


def stamp(path, lines, warn=""):
    """Draw the caption block into the bottom-right corner of the image, in place.

    `lines` are printed small and light; `warn` is printed under them in amber, and is
    where "uploaded from device" goes. Never raises: a photo that cannot be stamped is
    still a photo, and losing it to a drawing error would be the worse outcome.
    """
    try:
        from PIL import Image, ImageDraw
        img = Image.open(path)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        W, H = img.size
        px = max(13, int(W / 42))                      # legible on a phone and on A4
        f, fw = _font(px), _font(int(px * 1.05), bold=True)
        dr = ImageDraw.Draw(img, "RGBA")
        rows = [(t, f, (255, 255, 255, 235)) for t in lines if t]
        # A long warning must wrap, not run off the edge: the block is anchored to the
        # right, so one over-wide line pushes the whole caption past the left margin.
        limit = W * 0.62
        for part in _wrap(dr, warn, fw, limit) if warn else []:
            rows.append((part, fw, (253, 224, 71, 255)))
        pad, gap = int(px * 0.6), int(px * 0.35)
        wide = max(dr.textlength(t, font=ft) for t, ft, _ in rows)
        box_w = int(wide) + pad * 2
        box_h = len(rows) * (px + gap) + pad * 2 - gap
        x0, y0 = W - box_w - int(px * 0.5), H - box_h - int(px * 0.5)
        dr.rectangle([x0, y0, W - int(px * 0.5), H - int(px * 0.5)], fill=(0, 0, 0, 150))
        y = y0 + pad
        for t, ft, col in rows:
            dr.text((x0 + pad, y), t, font=ft, fill=col)
            y += px + gap
        img.save(path, quality=88)
        return True
    except Exception as e:
        _log.warning("[photos] could not stamp %s: %s", path, e)
        return False


def caption(job, user_name, src, exif, when=None):
    """The lines that go on the picture, and the warning line if there is one."""
    when = when or datetime.now()
    lines = [when.strftime("%d-%m-%Y %H:%M"),
             " · ".join(x for x in [job.get("jobid") or "", job.get("mcode") or ""] if x),
             user_name or ""]
    warn = ""
    if src != "camera":
        th, en = SRC_LABEL.get(src, SRC_LABEL["upload"])
        warn = f"{th} / {en}"
    # a picture whose own clock says it was taken well before the job was reported is
    # the one worth flagging, whatever button was used to attach it
    try:
        if exif.get("dt") and job.get("created_at"):
            t0 = datetime.strptime(exif["dt"][:19], "%Y-%m-%d %H:%M:%S")
            t1 = datetime.strptime(str(job["created_at"])[:19], "%Y-%m-%d %H:%M:%S")
            days = (t1 - t0).days
            if days >= 1:
                warn = (warn + " · " if warn else "") + f"ถ่ายก่อนแจ้งงาน {days} วัน / taken {days}d before the job"
    except Exception:
        pass
    return [x for x in lines if x], warn


def note(c, jid, kind, src, exif, uid, path):
    """Keep what we know about this photo, so a screen or a form can say it later."""
    try:
        c.execute("""CREATE TABLE IF NOT EXISTS photo_meta(
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, kind TEXT DEFAULT '',
            src TEXT DEFAULT '', exif_dt TEXT DEFAULT '', device TEXT DEFAULT '',
            path TEXT DEFAULT '', user_id INTEGER, created_at TEXT DEFAULT '')""")
        c.execute("DELETE FROM photo_meta WHERE job_id=? AND kind=?", (jid, kind))
        c.execute("""INSERT INTO photo_meta(job_id,kind,src,exif_dt,device,path,user_id,created_at)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (jid, kind, src, (exif or {}).get("dt", ""),
                   " ".join(x for x in [(exif or {}).get("make", ""),
                                        (exif or {}).get("model", "")] if x)[:60],
                   path, uid, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    except Exception as e:
        _log.warning("[photos] could not record where the photo came from: %s", e)


def meta_for(c, jid):
    """{kind: row} for one job — empty when the table is not there yet."""
    try:
        return {r["kind"]: dict(r) for r in c.execute(
            "SELECT * FROM photo_meta WHERE job_id=?", (jid,))}
    except Exception:
        return {}
