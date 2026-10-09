import os

APP_VERSION = "2.3.2"

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("BFLFP_DATA") or os.path.join(BASE, "data")
STATIC = os.path.join(BASE, "static")
UPLOADS = os.path.join(DATA, "uploads")
REPORTS = os.path.join(DATA, "Report")
DB_PATH = os.path.join(DATA, "cmms.db")

# Which copy of the system is this? "live" is the real factory data; anything else
# (e.g. "dev") is a sandbox with its own data folder, and the app says so on screen
# so nobody demos on production by accident. Set by Start DEV.bat.
ENV = (os.environ.get("CMMS_ENV") or "live").strip().lower()
IS_LIVE = ENV in ("live", "prod", "production")
VAPID_PEM = os.path.join(DATA, "vapid_private.pem")

PORT = int(os.environ.get("PORT", "8000"))
# ── Whose name is on the paper ───────────────────────────────────────────────────
# Three plants, three legal entities. Every form was being printed under the FOOD
# PRODUCTS name because the company was a single compiled-in constant, so a BFL or a
# Petcare sheet went out naming the wrong company — on a controlled document that QA
# files and an auditor reads. The name now follows the plant the MACHINE belongs to,
# keyed by the factory code in the factories table, with the old constant kept as the
# fallback for a job on no asset at all.
#
# ⚠ The Thai names below are the legal entity names as far as we know them. Check them
# against the company registration before this goes out on a filed document — each is
# one env var away from being corrected without a build.
COMPANY_TH = os.environ.get("COMPANY_TH", "บริษัท บลูฟาโล่ ฟู้ด โปรดักส์ จำกัด")
COMPANY_TH_BY_CODE = {
    "BFL": os.environ.get("COMPANY_TH_BFL", "บริษัท บลูฟาโล่ จำกัด"),
    "FP":  os.environ.get("COMPANY_TH_FP",  "บริษัท บลูฟาโล่ ฟู้ด โปรดักส์ จำกัด"),
    "PC":  os.environ.get("COMPANY_TH_PC",  "บริษัท บลูฟาโล่ เพ็ทแคร์ จำกัด"),
}
COMPANY_EN_BY_CODE = {
    "BFL": os.environ.get("COMPANY_EN_BFL", "Bluefalo Company Limited"),
    "FP":  os.environ.get("COMPANY_EN_FP",  "Bluefalo Food Products Company Limited"),
    "PC":  os.environ.get("COMPANY_EN_PC",  "Bluefalo Petcare Company Limited"),
}
FORM_CODE = os.environ.get("FORM_CODE", "F-SP-ENG02-03 Rev.01")
# The PM checklist record carries its own number. QA owns it, so it is an env var
# rather than a literal: changing a controlled document number must not need a build.
PM_FORM_CODE = os.environ.get("PM_FORM_CODE", "F-SP-ENG02-05 Rev.01")
PUSH_CONTACT = os.environ.get("PUSH_CONTACT", "mailto:production@bluefalo-group.com")

os.makedirs(UPLOADS, exist_ok=True)
os.makedirs(REPORTS, exist_ok=True)
