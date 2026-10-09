"""BFLFP CMMS smoke test — the regression gate.

Run BEFORE merging any module change and AFTER every deploy:
    python tests/smoke.py

Spawns the app on port 8765 with a THROWAWAY database (your real data/ is
untouched), exercises every module end-to-end, prints PASS/FAIL per check.
Exit code 0 = safe to release. Any failure = a module broke another module.
"""
import json, os, subprocess, sys, tempfile, time, urllib.request, urllib.error

PORT = 8765
BASE = f"http://localhost:{PORT}"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


class Client:
    def __init__(self):
        self.cookie = None

    def req(self, method, path, body=None, raw=False):
        r = urllib.request.Request(BASE + path, method=method,
                                   headers={"Content-Type": "application/json"})
        if self.cookie:
            r.add_header("Cookie", self.cookie)
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urllib.request.urlopen(r, data=data, timeout=10) as resp:
                ck = resp.headers.get("Set-Cookie")
                if ck:
                    self.cookie = ck.split(";")[0]
                payload = resp.read()
                return resp.status, payload if raw else (json.loads(payload) if payload else {})
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except Exception:
                return e.code, {}

    def login(self, u, pw="1234"):
        return self.req("POST", "/api/login", {"username": u, "password": pw})


def main():
    tmp = tempfile.mkdtemp(prefix="bflfp_smoke_")
    env = {**os.environ, "BFLFP_DATA": tmp, "PORT": str(PORT)}
    # SAFETY: never run smoke against the production database.
    # Default = throwaway SQLite. To verify PostgreSQL, point SMOKE_DATABASE_URL
    # at a SCRATCH database (e.g. cmms_test) — never at cmms itself.
    env.pop("DATABASE_URL", None)
    if os.environ.get("SMOKE_DATABASE_URL"):
        env["DATABASE_URL"] = os.environ["SMOKE_DATABASE_URL"]
        print(f"  (smoke running against {env['DATABASE_URL'].split('@')[-1]})")
    env["CMMS_TEST_CAPTCHA"] = "1"   # captcha endpoint reveals answer in test mode
    proc = subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        for _ in range(40):
            time.sleep(0.5)
            try:
                urllib.request.urlopen(BASE + "/api/health", timeout=2)
                break
            except Exception:
                pass
        print("== health / version ==")
        st, h = Client().req("GET", "/api/health")
        check("health responds", st == 200 and h.get("ok"))
        check("version present", bool(h.get("version")))

        print("== auth ==")
        bad = Client()
        st, _ = bad.login("admin1", "wrong")
        check("wrong password rejected (401)", st == 401)

        print("== brute-force protection ==")
        def crq(resp):
            return isinstance(resp.get("detail"), dict) and resp["detail"].get("captcha_required")
        bf = Client()
        bf.login("bruteuser", "x1")
        bf.login("bruteuser", "x2")
        st, d = bf.login("bruteuser", "x3")
        check("captcha demanded after 2 failures", bool(crq(d)), str(d)[:60])
        st, cap = bf.req("GET", "/api/captcha")
        check("captcha issued (test mode shows answer)", st == 200 and cap.get("answer"))
        st, d = bf.req("POST", "/api/login",
                       {"username": "bruteuser", "password": "x4",
                        "captcha_token": cap["token"], "captcha_answer": "WRONG"})
        check("wrong captcha rejected", st == 401 and bool(crq(d)))
        # real user recovers through the captcha path
        p2 = Client()
        p2.login("planner1", "bad1")
        p2.login("planner1", "bad2")
        st, d = p2.login("planner1", "1234")
        check("correct password still blocked without captcha", st == 401 and bool(crq(d)))
        st, cap2 = p2.req("GET", "/api/captcha")
        st, d = p2.req("POST", "/api/login",
                       {"username": "planner1", "password": "1234",
                        "captcha_token": cap2["token"], "captcha_answer": cap2["answer"]})
        check("correct captcha + password logs in", st == 200)
        st, _ = Client().login("planner1")
        check("failures cleared after success (plain login ok)", st == 200)
        op, pl, t1, mg, ad = Client(), Client(), Client(), Client(), Client()
        ok = all(c.login(u)[0] == 200 for c, u in
                 [(op, "op1"), (pl, "planner1"), (t1, "tech1"), (mg, "manager1"), (ad, "admin1")])
        check("all role logins", ok)

        print("== master data / admin ==")
        st, machines = ad.req("GET", "/api/admin/machines")
        check("machines seeded (10)", st == 200 and len(machines) == 10)
        st, _ = ad.req("POST", "/api/admin/users",
                       {"username": "smoketech", "name": "Smoke", "role": "technician", "password": "9999"})
        check("admin creates user", st == 200)
        st, _ = Client().login("smoketech", "9999")
        check("new user can login (hashed pw)", st == 200)
        st, _ = op.req("GET", "/api/admin/users")
        check("operator blocked from admin (403)", st == 403)

        print("== PM auto-generation ==")
        st, _ = pl.req("GET", "/api/bootstrap")
        st, r = pl.req("GET", "/api/jobs?view=recent")
        pm_auto = [j for j in r["jobs"] if j["jobsource"] == "PM-Auto"]
        check("PM jobs auto-created on planner login", len(pm_auto) == 10, f"got {len(pm_auto)}")

        print("== shift log ==")
        st, _ = op.req("POST", "/api/shiftlogs",
                       {"machine_id": 2, "shift": "เช้า", "planned_min": 480, "output": 50000, "good": 49000})
        check("shiftlog saved", st == 200)
        st, sl = op.req("GET", "/api/shiftlogs")
        check("shiftlog listed with reject auto", sl and sl[0]["reject"] == 1000)

        print("== BD lifecycle ==")
        st, bd = op.req("POST", "/api/jobs",
                        {"jobtype": "BD", "machine_id": 2, "descr": "smoke jam", "priority": 3})
        check("BD created Reported", st == 200 and bd["status"] == "Reported")
        jid = bd["id"]
        st, _ = t1.req("POST", "/api/segments/start", {"job_id": jid, "seg_type": "work"})
        check("segment start", st == 200)
        time.sleep(1)
        st, _ = t1.req("POST", "/api/segments/stop",
                       {"action": "pause", "reason": "รออะไหล่", "progress": 50})
        check("pause with reason", st == 200)
        st, r = t1.req("GET", "/api/jobs?view=recent")
        j = [x for x in r["jobs"] if x["id"] == jid][0]
        check("status Paused + progress 50", j["status"] == "Paused" and j["progress"] == 50)
        t1.req("POST", "/api/segments/start", {"job_id": jid, "seg_type": "work"})
        time.sleep(1)
        st, _ = t1.req("POST", "/api/segments/stop",
                       {"action": "finish", "problem": "p", "root_cause": "r", "solution": "s",
                        "fault_category": "เครื่องกล (Mechanical)", "fault_component": "Seal/ปะเก็น",
                        "maint_action": "เปลี่ยนอะไหล่ (Replace)"})
        check("finish with fault codes", st == 200)
        st, d = pl.req("GET", f"/api/jobs/{jid}/detail")
        check("detail: segments recorded", len(d["segments"]) == 2)
        check("detail: fault codes saved", d["job"]["fault_component"] == "Seal/ปะเก็น")
        check("done_at set", bool(d["job"]["done_at"]))

        print("== media + approve ==")
        png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        st, m = t1.req("POST", f"/api/jobs/{jid}/media", {"kind": "after", "data": png})
        check("photo upload", st == 200 and m["path"].startswith("/uploads/"))
        st, m = pl.req("POST", f"/api/jobs/{jid}/media", {"kind": "sign_inspector", "data": png})
        check("signature save", st == 200)
        st, j = pl.req("PATCH", f"/api/jobs/{jid}", {"status": "Rework"})
        check("rework increments counter", j.get("rework_count") == 1)
        st, j = pl.req("PATCH", f"/api/jobs/{jid}", {"status": "Done", "cleared_worksite": 1})
        check("approved Done", j["status"] == "Done")
        st, _ = pl.req("PATCH", "/api/jobs/99999", {"status": "Done"})
        check("missing job → 404 not crash", st == 404)

        print("== chatter (Odoo-style) ==")
        st, msg = pl.req("POST", "/api/chat/messages",
                         {"job_id": jid, "text": "note test", "kind": "note"})
        check("log note (silent)", st == 200 and msg["kind"] == "note")
        st, _ = pl.req("POST", "/api/chat/activities",
                       {"job_id": jid, "act_type": "todo", "summary": "smoke act",
                        "assignee": 3, "due_date": "2026-12-31"})
        check("activity scheduled", st == 200)
        st, acts = t1.req("GET", "/api/chat/activities?mine=1")
        check("assignee sees activity", any(a["summary"] == "smoke act" for a in acts))
        st, _ = t1.req("PATCH", f"/api/chat/activities/{acts[0]['id']}", {"done": 1})
        check("activity done", st == 200)
        st, tl = pl.req("GET", f"/api/chat/messages?job_id={jid}")
        kinds = [m["kind"] for m in tl["messages"]]
        check("timeline has system lines", "system" in kinds)
        st, chans = pl.req("GET", "/api/chat/channels")
        check("channels seeded (3)", len(chans) == 3)
        sysc = [c for c in chans if c["kind"] == "system"][0]
        st, feed = pl.req("GET", f"/api/chat/messages?channel_id={sysc['id']}")
        check("BD auto-posted to breakdown feed", any("smoke jam" in m["text"] for m in feed["messages"]))

        print("== KPI engine ==")
        st, k = mg.req("GET", "/api/kpi")
        pk = k["plant"]
        m2 = [x for x in k["machines"] if x["machine_id"] == 2][0]
        check("OEE computed", m2["oee"] is not None and 80 < m2["oee"] < 90, f"oee={m2['oee']}")
        check("quality 98", abs((m2["quality"] or 0) - 98.0) < 0.2)
        check("bd counted", m2["bd_count"] == 1)
        check("FTFR reflects rework (0%)", pk["ftfr"] == 0.0, f"ftfr={pk['ftfr']}")
        check("targets present", k["targets"]["oee"] == 65)
        check("top failures has Seal", any("Seal" in f["comp"] for f in pk["top_failures"]))
        st, tr = mg.req("GET", "/api/kpi/trend?days=7")
        check("trend series 7 days", len(tr["series"]) == 7)
        st, h = mg.req("GET", "/api/machines/2/history")
        check("machine history (jobs+logs)", len(h["jobs"]) >= 1 and len(h["shiftlogs"]) == 1)

        print("== PDFs ==")
        st, pdf = pl.req("GET", f"/api/jobs/{jid}/pdf", raw=True)
        check("repair-form PDF", st == 200 and pdf[:5] == b"%PDF-")
        st, pdf = mg.req("GET", "/api/reports/daily", raw=True)
        check("daily report PDF", st == 200 and pdf[:5] == b"%PDF-")

        print("== push ==")
        st, pk2 = t1.req("GET", "/api/push/key")
        check("push key issued", st == 200 and pk2["enabled"] and len(pk2["key"] or "") > 80)

        print("== v3 schema ==")
        st, h3 = Client().req("GET", "/api/health")
        check("26 tables in schema", st == 200 and h3.get("tables") == 26,
              f"got {h3.get('tables')}")

        print("== factory selector (login) ==")
        st, facs = Client().req("GET", "/api/factories")
        check("factories listed (3)", st == 200 and len(facs) == 3)
        st, d = Client().req("POST", "/api/login",
                             {"username": "op1", "password": "1234", "factory_id": 1})
        check("wrong factory rejected for operator", st == 401)
        c2 = Client()
        st, d = c2.req("POST", "/api/login",
                       {"username": "op1", "password": "1234", "factory_id": 2})
        check("own factory login ok + factory in response",
              st == 200 and d.get("factory_id") == 2 and d.get("factory_code"))
        st, d = Client().req("POST", "/api/login",
                             {"username": "admin1", "password": "1234", "factory_id": 3})
        check("admin may enter any factory", st == 200 and d.get("factory_id") == 3)
        st, d = c2.req("GET", "/api/me")
        check("/api/me carries factory context", st == 200 and d.get("factory_id") == 2)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    failed = [r for r in results if not r[1]]
    print(f"\n{'='*46}\n  {len(results)-len(failed)}/{len(results)} checks passed", end="")
    if failed:
        print("  — FAILED:")
        for name, _, det in failed:
            print(f"   ✗ {name} {det}")
        sys.exit(1)
    print("  — ALL GREEN, safe to release ✅")


if __name__ == "__main__":
    main()
