import os

APP_VERSION = "3.0.0"

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("BFLFP_DATA") or os.path.join(BASE, "data")
STATIC = os.path.join(BASE, "static")
UPLOADS = os.path.join(DATA, "uploads")
REPORTS = os.path.join(DATA, "Report")
DB_PATH = os.path.join(DATA, "cmms.db")
VAPID_PEM = os.path.join(DATA, "vapid_private.pem")

PORT = int(os.environ.get("PORT", "8000"))
COMPANY_TH = os.environ.get("COMPANY_TH", "บริษัท บลูฟาโล่ ฟู้ด โปรดักส์ จำกัด")
FORM_CODE = os.environ.get("FORM_CODE", "F-SP-ENG02-03 Rev.01")
PUSH_CONTACT = os.environ.get("PUSH_CONTACT", "mailto:production@bluefalo-group.com")

os.makedirs(UPLOADS, exist_ok=True)
os.makedirs(REPORTS, exist_ok=True)
