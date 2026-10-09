import atexit
import os
import socket
import subprocess
import sys
import time
import uvicorn

from server.config import PORT

HERE = os.path.dirname(os.path.abspath(__file__))
TUNNEL_SCRIPT = os.path.join(HERE, "deploy", "start_tunnel.py")


def public_url():
    """The address the phones are bookmarked at — read, never hard-coded.

    `start_tunnel.py` points the Worker at whatever `CF_WORKER_URL` says, and that
    value differs per machine: the live server has none and takes the live default,
    this development PC sets the -dev Worker in `deploy/secrets.env`. Printing a
    literal here meant the banner carried one machine's address onto the other the
    moment run.py was copied across. Same source, same answer.
    """
    url = os.environ.get("CF_WORKER_URL")
    if not url:
        try:
            with open(os.path.join(HERE, "deploy", "secrets.env"), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("CF_WORKER_URL") and "=" in line:
                        url = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except Exception:
            pass
    return url or "https://cmms.bflgroup.workers.dev"


def start_tunnel():
    """Start the Cloudflare tunnel alongside the app, in the same window.

    Returns the child process, or None if skipped / unavailable.
    Set env var CMMS_TUNNEL=0 to run localhost-only (no public URL).
    The app always starts even if the tunnel can't (e.g. no internet,
    cloudflared not installed) — it just won't be reachable from phones.
    """
    if os.environ.get("CMMS_TUNNEL", "1") == "0":
        print("  Tunnel disabled (CMMS_TUNNEL=0) — localhost only.\n")
        return None
    if not os.path.exists(TUNNEL_SCRIPT):
        print("  ! Tunnel script not found; starting app only.\n")
        return None
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen([sys.executable, TUNNEL_SCRIPT], **kwargs)
    except Exception as e:
        print(f"  ! Could not start tunnel ({e}); starting app only.\n")
        return None


def stop_tunnel(proc):
    """Kill the tunnel and its cloudflared child so no stale tunnel is left."""
    if not proc or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True)
        else:
            proc.terminate()
            proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "localhost"

    tunnel = start_tunnel()
    atexit.register(stop_tunnel, tunnel)

    lines = ["\n  BFLFP CMMS running:",
             f"   This PC : http://localhost:{PORT}",
             f"   Phones  : http://{ip}:{PORT}  (same wifi)"]
    if tunnel:
        # The tunnel can die in its first second — no API token, no cloudflared.exe,
        # no internet. Give it that second, then say what is actually true: printing a
        # public URL that nobody can reach sends people hunting the wrong problem.
        time.sleep(1.5)
        if tunnel.poll() is not None:
            tunnel = None
            lines.append("   Public  : NOT running — the tunnel exited (its reason is printed above)")
        else:
            lines.append(f"   Public  : {public_url()}  (via tunnel)")
    try:
        from server.push import PUSH_OK
        lines.append("   Push    : " + ("ENABLED" if PUSH_OK else "DISABLED (pywebpush not in THIS Python)"))
    except Exception as e:
        lines.append(f"   Push    : error - {e}")
    reload = os.environ.get("CMMS_RELOAD", "1") != "0"
    lines.append("   Reload  : " + ("ON — edits to server/*.py apply automatically"
                                     if reload else "OFF (set CMMS_RELOAD=1 to enable)"))
    print("\n".join(lines) + "\n")

    try:
        # reload watches ONLY server/*.py — so the SQLite DB, uploads and .venv never trigger restarts.
        # The tunnel started above lives in this supervisor process and persists across reloads.
        # A screen may be holding a question open for up to 25s waiting for news.
        # Without a cap, uvicorn sits on "waiting for connections to close" until it
        # answers — a restart that hangs, with the site down while it does.
        uvicorn.run("server.app:app", host="0.0.0.0", port=PORT,
                    timeout_graceful_shutdown=5,
                    reload=reload, reload_dirs=[os.path.join(HERE, "server")])
    finally:
        stop_tunnel(tunnel)
