"""Database access layer (v2 — SQLAlchemy engine underneath).

Modules keep calling db(), execute with `?` placeholders, and commit —
identical code runs on SQLite (dev/pilot) and PostgreSQL (production).
"""
import hashlib, secrets
from datetime import datetime, date

from .database import get_conn, create_schema, engine


def db():
    return get_conn()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today():
    return date.today().isoformat()


def day_range(d):
    """Portable 'created during day d' boundaries for TEXT timestamps."""
    return d + " 00:00:00", d + " 23:59:59"


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def check_pw(pw, stored):
    if "$" not in (stored or ""):
        return pw == stored
    salt = stored.split("$", 1)[0]
    return hash_pw(pw, salt) == stored


def job_row(c, jid):
    r = c.execute("""SELECT j.*, m.code mcode, m.name mname, u.name lead_name
        FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
        LEFT JOIN users u ON u.id=j.lead_tech WHERE j.id=?""", (jid,)).fetchone()
    if not r:
        from fastapi import HTTPException
        raise HTTPException(404, f"job {jid} not found")
    return dict(r)


def role_ids(c, *roles):
    q = f"SELECT id FROM users WHERE active=1 AND role IN ({','.join('?' * len(roles))})"
    return [r["id"] for r in c.execute(q, roles)]


def user_names(c):
    return {str(r["id"]): r["name"] for r in c.execute("SELECT id,name FROM users")}


def init():
    create_schema()
    c = db()
    try:
        if not c.execute("SELECT id FROM factories LIMIT 1").fetchone():
            c.executemany("INSERT INTO factories(code,name,form_code) VALUES(?,?,?)", [
                ("BFL", "Bluefalo Main Plant", "SD-SP-ENG02-01"),
                ("FP", "Bluefalo Food Products (Wet Food)", "F-SP-ENG02-03 Rev.01"),
                ("PC", "Bluefalo Petcare", "PC-ENG-01"),
            ])
        if not c.execute("SELECT id FROM users LIMIT 1").fetchone():
            seed(c)
        if not c.execute("SELECT id FROM channels LIMIT 1").fetchone():
            c.executemany("INSERT INTO channels(name,kind) VALUES(?,?)", [
                ("ทีมช่าง (Maintenance team)", "team"),
                ("ไลน์ผลิต 1 (Line 1)", "team"),
                ("🔴 Breakdown feed", "system"),
            ])
        c.commit()
    finally:
        c.close()


def seed(c):
    users = [
        ("admin1", "ผู้ดูแลระบบ (Admin)", "admin"),
        ("planner1", "คุณวางแผน (Planner)", "planner"),
        ("tech1", "สมชาย (Somchai)", "technician"),
        ("tech2", "สมศักดิ์ (Somsak)", "technician"),
        ("op1", "โอเปอเรเตอร์ไลน์ 1", "operator"),
        ("manager1", "ผู้จัดการโรงงาน (Manager)", "manager"),
    ]
    for u, n, r in users:
        c.execute("INSERT INTO users(username,password,name,role,active,factory_id) VALUES(?,?,?,?,1,2)",
                  (u, hash_pw("1234"), n, r))
    machines = [
        # code, name, rate, crit, line, pm_days, kpi_class, approved
        ("W01MX01", "เครื่องผสม (Mixer) #1", 0, "A", "Line 1", 7, "BATCH", 1),
        ("W01LP01", "เครื่องบรรจุซอง (Sachet filler) #1", 120, "A", "Line 1", 7, "OEE", 1),
        ("W01SM01", "เครื่องปิดฝากระป๋อง (Can seamer) #1", 120, "A", "Line 1", 7, "OEE", 1),
        ("W01RT01", "หม้อฆ่าเชื้อ (Retort) #1", 0, "A", "Line 1", 30, "BATCH", 1),
        ("W01RT02", "หม้อฆ่าเชื้อ (Retort) #2", 0, "A", "Line 1", 30, "BATCH", 1),
        ("W01AC01", "ปั๊มลมสกรู (Air compressor) #1", 0, "B", "Utilities", 7, "AVAIL", 1),
        ("W01BL01", "หม้อไอน้ำ (Boiler) #1", 0, "A", "Utilities", 7, "AVAIL", 1),
        ("W01CH01", "ตู้แช่ทำความเย็น (Chiller) #1", 0, "B", "Utilities", 7, "AVAIL", 1),
        ("W01CV01", "สายพานลำเลียง (Conveyor) #1", 0, "C", "Line 1", 30, "AVAIL", 1),
        ("W01LB01", "เครื่องติดฉลาก (Labeler) #1", 100, "B", "Line 1", 7, "OEE", 1),
    ]
    for m in machines:
        c.execute("""INSERT INTO machines(code,name,ideal_rate,criticality,line,pm_freq_days,
            kpi_class,kpi_approved,active,factory_id) VALUES(?,?,?,?,?,?,?,?,1,2)""", m)
