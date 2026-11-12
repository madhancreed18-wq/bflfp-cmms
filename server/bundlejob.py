"""b5xx — builds report bundles OUTSIDE the web server.

Run as:  python -m server.bundlejob <spec.json>

The web server starts this as a separate, low-priority process, so drawing a hundred
PDFs never slows down the phones, the dashboard or anybody else's request: the server
only reads this process's progress lines. It reads the database and writes PDF files;
it never changes a database row (opening documents is not issuing them).

spec.json:
  {"mode": "bundle", "ids": [plan_reports ids], "user": {...}, "out": "<pdf path>"}
  {"mode": "warm"}                       nightly: redraw every repair form that changed
Progress on stdout, one line each:
  P <done> <total> <missing>     after each document
  M                              merging into one file
  OK <path>                      finished
  ERR <message>                  failed
"""
import json
import os
import re
import sys
from contextlib import closing


def _low_priority():
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000)   # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(10)
    except Exception:
        pass


def say(*a):
    print(*a, flush=True)


def row_pdf(c, r):
    """The PDF behind one shelf row — reused when current, redrawn when not.
    Same documents as reports._row_pdf, but with no database writes."""
    from .reports import archive_found, archive_path, build_maintenance_form, _done_rows
    from .jobform import ensure_job_form
    kind = r.get("kind") or "plan"
    if kind == "done":
        fn = archive_found(r["plan_date"], r["factory_id"])
        if not fn and _done_rows(r["plan_date"], r["factory_id"]):
            fn = build_maintenance_form(r["plan_date"], archive_path(r["plan_date"], r["factory_id"]),
                                        r["factory_id"])
        return fn
    if kind == "jobform":
        m = re.search(r"/api/jobs/(\d+)/pdf", r.get("path") or "")
        if not m:
            return ""
        jid = int(m.group(1))
        job = c.execute("SELECT id, factory_id FROM jobs WHERE id=?", (jid,)).fetchone()
        if not job or (job["factory_id"] and job["factory_id"] != r["factory_id"]):
            return ""
        return ensure_job_form(jid)[0]
    return ""


def bundle(spec):
    from .db import db
    from .reports import _shelf_rows, _pdf_writer
    with closing(db()) as c:
        rows = _shelf_rows(c, spec["ids"], spec["user"])
        if not rows:
            raise ValueError("none of those documents belong to this plant")
        kinds = {r.get("kind") or "plan" for r in rows}
        if len(kinds) > 1 or "plan" in kinds:
            raise ValueError("pick repair forms or daily reports only, one kind at a time")
        files, missing = [], 0
        say("P", 0, len(rows), 0)
        for i, r in enumerate(rows, 1):
            try:
                fn = row_pdf(c, r)
            except Exception as e:
                say("W", f"document {r.get('id')}: {e}")
                fn = ""
            if fn and os.path.isfile(fn):
                files.append(fn)
            else:
                missing += 1
            say("P", i, len(rows), missing)
    if not files:
        raise ValueError("none of the selected documents could be found")
    say("M")
    w = _pdf_writer()
    for fn in files:
        w.append(fn)
    tmp = spec["out"] + ".part"
    with open(tmp, "wb") as fh:
        w.write(fh)
    os.replace(tmp, spec["out"])
    say("OK", spec["out"])


def warm(spec):
    """Nightly: every repair form on the shelf whose content changed is redrawn now,
    so tomorrow's bundles find them ready. Unchanged forms cost one read each."""
    from .db import db
    from .jobform import ensure_job_form
    with closing(db()) as c:
        ids = []
        for r in c.execute("SELECT path FROM plan_reports WHERE kind='jobform'"):
            m = re.search(r"/api/jobs/(\d+)/pdf", r["path"] or "")
            if m:
                ids.append(int(m.group(1)))
    built = bad = 0
    say("P", 0, len(ids), 0)
    for i, jid in enumerate(ids, 1):
        try:
            if ensure_job_form(jid)[1]:
                built += 1
        except Exception as e:
            bad += 1
            say("W", f"job {jid}: {e}")
        say("P", i, len(ids), bad)
    say("OK", f"{built} redrawn, {len(ids) - built - bad} already current, {bad} failed")


def main():
    _low_priority()
    try:
        spec = json.load(open(sys.argv[1], encoding="utf-8"))
        (warm if spec.get("mode") == "warm" else bundle)(spec)
    except Exception as e:
        say("ERR", str(e).replace("\n", " "))
        sys.exit(1)


if __name__ == "__main__":
    main()
