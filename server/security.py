"""Login protection — brute-force defense, fully self-hosted.

Policy:
- 2 failed passwords (per username+IP, 15-min window)  → captcha required
- 10 failed passwords in the window                    → locked 15 minutes
- every failed attempt is slowed by a small delay
Captcha images are generated locally with Pillow — no Google, no internet
needed, nothing leaves the plant.
"""
import base64, io, os, random, secrets, time

WINDOW = 900          # seconds of memory for failures
CAPTCHA_AFTER = 2     # failures before captcha is demanded
LOCK_AFTER = 10       # failures before temporary lockout
LOCK_SECONDS = 900

_FAILS = {}           # key -> [timestamps]
_CAPTCHAS = {}        # token -> (answer, expires_at)

_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O/1/I/L


def _key(username, ip):
    return (username or "").strip().lower() + "|" + (ip or "")


def _trim(key):
    cutoff = time.time() - WINDOW
    _FAILS[key] = [t for t in _FAILS.get(key, []) if t > cutoff]
    if not _FAILS[key]:
        _FAILS.pop(key, None)


IP_CAPTCHA_AFTER = 4     # attacker rotating usernames from one IP still hits captcha
IP_LOCK_AFTER = 20


def record_fail(username, ip):
    for k in (_key(username, ip), _key("", ip)):     # per-user AND per-IP
        _trim(k)
        _FAILS.setdefault(k, []).append(time.time())


def clear_fails(username, ip):
    _FAILS.pop(_key(username, ip), None)
    _FAILS.pop(_key("", ip), None)   # legit user on shared/NAT IP unblocks colleagues


def fail_count(username, ip):
    k = _key(username, ip)
    _trim(k)
    return len(_FAILS.get(k, []))


def locked_for(username, ip):
    """Seconds remaining of lockout, or 0. Checks user-level and IP-level."""
    for k, limit in ((_key(username, ip), LOCK_AFTER), (_key("", ip), IP_LOCK_AFTER)):
        _trim(k)
        fails = _FAILS.get(k, [])
        if len(fails) >= limit:
            return max(0, int(fails[-1] + LOCK_SECONDS - time.time()))
    return 0


def captcha_required(username, ip):
    return (fail_count(username, ip) >= CAPTCHA_AFTER
            or fail_count("", ip) >= IP_CAPTCHA_AFTER)


def _font(size):
    from PIL import ImageFont
    for p in [r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\tahomabd.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def new_captcha():
    """Returns (token, data_url, answer). Answer stored server-side, 5-min TTL."""
    from PIL import Image, ImageDraw
    answer = "".join(random.choices(_CHARS, k=5))
    w, h = 190, 62
    img = Image.new("RGB", (w, h), (246, 250, 253))
    d = ImageDraw.Draw(img)
    for _ in range(6):                                  # noise lines
        d.line([(random.randint(0, w), random.randint(0, h)) for _ in range(2)],
               fill=(random.randint(140, 200),) * 3, width=1)
    f = _font(34)
    for i, ch in enumerate(answer):                     # jittered characters
        x = 14 + i * 34 + random.randint(-3, 3)
        y = 8 + random.randint(-4, 6)
        col = (random.randint(15, 60), random.randint(60, 110), random.randint(140, 190))
        d.text((x, y), ch, font=f, fill=col)
    for _ in range(120):                                # noise dots
        d.point((random.randint(0, w - 1), random.randint(0, h - 1)),
                fill=(random.randint(120, 210),) * 3)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    token = secrets.token_hex(10)
    # keep the store small + drop expired
    now_t = time.time()
    for t in [t for t, (_a, exp) in _CAPTCHAS.items() if exp < now_t]:
        _CAPTCHAS.pop(t, None)
    _CAPTCHAS[token] = (answer, now_t + 300)
    data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    return token, data_url, answer


def verify_captcha(token, answer):
    """Single-use, 5-minute validity, case-insensitive."""
    entry = _CAPTCHAS.pop(token or "", None)
    if not entry:
        return False
    real, exp = entry
    return time.time() <= exp and (answer or "").strip().upper() == real
