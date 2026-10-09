"""Start a Cloudflare quick tunnel and point the Worker at it automatically.

Run this INSTEAD of running cloudflared by hand:
    python deploy/start_tunnel.py

It launches cloudflared, waits for the random trycloudflare URL, then updates
the Worker's KV key so your fixed workers.dev URL forwards to it.

CREDENTIALS
    Put the token in  deploy/secrets.env  (this folder), one line:

        CF_API_TOKEN=cfat_your-token-here

    That file is in .gitignore, so the token never reaches git, and unlike setx it
    survives a reboot and needs no new terminal. A real CF_API_TOKEN environment
    variable, if one is set, takes precedence.

Check the credentials without starting anything — the token is never printed:
    python deploy/start_tunnel.py --check

One-time setup:
1. Cloudflare dashboard -> Workers & Pages -> KV -> Create namespace "TUNNEL".
   Open your Worker -> Settings -> Bindings -> Add -> KV namespace,
   variable name TUNNEL, select the namespace. Note the namespace ID.
2. My Profile -> API Tokens -> Create Token -> Custom:
   Permission: Account / Workers KV Storage / Edit. Copy the token.
3. Account ID: dashboard right sidebar (Workers & Pages overview).
"""
import re, subprocess, sys, time, urllib.request, urllib.error, json, os

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(HERE, "secrets.env")


def _load_secrets():
    """Read deploy/secrets.env into the environment.

    A factory PC gets rebooted and the shell gets reopened by whoever is on shift, so
    a token that lives only in `setx` is a token somebody will lose. This file sits
    next to the script, is in .gitignore, and needs no terminal restart. Anything
    already in the real environment wins, so a machine that does use setx is unaffected.
    """
    if not os.path.exists(SECRETS):
        return
    with open(SECRETS, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_secrets()

CONFIG = {
    "ACCOUNT_ID":      os.environ.get("CF_ACCOUNT_ID") or "5477580ec14963468641e500bce044b3",
    "KV_NAMESPACE_ID": os.environ.get("CF_KV_NAMESPACE_ID") or "17cacb284a204e5999613f0e0af31cf9",
    "API_TOKEN":       os.environ.get("CF_API_TOKEN") or "PASTE-YOUR-TOKEN-HERE",
    "CLOUDFLARED":     r"C:\cloudflared\cloudflared.exe",
    "LOCAL":           "http://localhost:8000",
    # the fixed address the phones are bookmarked at — printed on every start so
    # nobody has to remember it
    "WORKER_URL":      os.environ.get("CF_WORKER_URL") or "https://cmms.bflgroup.workers.dev",
    # QUIC rides on UDP. Plenty of factory networks throttle or drop it, which shows
    # up as "timeout: no recent network activity" every ~30s and a tunnel that
    # reconnects all day. http2 is the documented fallback and is steady on those
    # networks. Set CF_PROTOCOL=quic to go back.
    "PROTOCOL":        os.environ.get("CF_PROTOCOL") or "http2",
}

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def set_kv(url, tries=4):
    """Point the Worker at this tunnel. Never raises — a failure here must not take
    the tunnel down with it, because the tunnel is the factory's connection."""
    acct = CONFIG["ACCOUNT_ID"].strip()
    ns   = CONFIG["KV_NAMESPACE_ID"].strip()
    tok  = CONFIG["API_TOKEN"].strip()
    api  = (f"https://api.cloudflare.com/client/v4/accounts/{acct}"
            f"/storage/kv/namespaces/{ns}/values/target")
    for n in range(1, tries + 1):
        req = urllib.request.Request(api, data=url.encode(), method="PUT",
            headers={"Authorization": f"Bearer {tok}",
                     "Content-Type": "text/plain"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                ok = json.loads(r.read()).get("success")
            print(f"  Worker target updated -> {url}" if ok else "  ! KV update failed")
            return bool(ok)
        except urllib.error.HTTPError as e:
            # A 4xx is a wrong token or a wrong id. Retrying cannot fix that, so say
            # what Cloudflare said and stop.
            print(f"  ! KV update failed: HTTP {e.code}")
            print("    Cloudflare said: " + e.read().decode(errors="replace")[:500])
            return False
        except Exception as e:
            # A reset connection, a DNS blip, a timeout. These come and go — wait and
            # try again, and if it still will not go through, carry on anyway: the
            # tunnel itself is up, only the redirect is stale.
            if n == tries:
                print(f"  ! KV update failed after {tries} tries: {e}")
                print(f"    The tunnel is still up at {url}")
                print("    Phones on the fixed URL will reach the old target until this succeeds.")
                return False
            wait = 2 * n
            print(f"  KV update attempt {n} failed ({e.__class__.__name__}) — retrying in {wait}s")
            time.sleep(wait)
    return False


def mask(tok):
    """Enough of the token to tell two apart, never enough to use."""
    t = (tok or "").strip()
    return f"{t[:9]}...{t[-4:]}" if len(t) > 16 else "(too short to be a token)"


def check():
    """Prove the token works, without the token ever appearing on screen.

    A token pasted into a terminal ends up in the scrollback, in the shell history
    file, and in any screenshot of that window. This reads it from secrets.env and
    prints only a masked form, so checking a token can never leak it.
    """
    tok  = CONFIG["API_TOKEN"].strip()
    acct = CONFIG["ACCOUNT_ID"].strip()
    ns   = CONFIG["KV_NAMESPACE_ID"].strip()
    src  = SECRETS if os.path.exists(SECRETS) else "the environment"
    print(f"  Token   : {mask(tok)}   (from {src})")

    def call(url, method="GET"):
        req = urllib.request.Request(url, method=method,
                                     headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    # Cloudflare has two kinds of token and two verify endpoints, and each rejects the
    # other kind with a flat "Invalid API Token" — which reads like a bad token when it
    # is really the wrong door. cfat_ is account-owned, cfut_ is user-owned.
    account_first = tok.startswith("cfat_")
    doors = [(f"https://api.cloudflare.com/client/v4/accounts/{acct}/tokens/verify", "account"),
             ("https://api.cloudflare.com/client/v4/user/tokens/verify", "user")]
    if not account_first:
        doors.reverse()

    verified = None
    for url, kind in doors:
        for method in ("GET", "POST"):
            try:
                d = call(url, method)
            except urllib.error.HTTPError as e:
                if e.code == 405:
                    continue            # this endpoint wants the other verb
                break
            except Exception as e:
                print(f"  Verify  : could not reach Cloudflare - {e}")
                return 1
            if d.get("success"):
                verified = (kind, (d.get("result") or {}).get("status", "ok"))
            break
        if verified:
            break

    if verified:
        print(f"  Verify  : {verified[1]}  ({verified[0]}-owned token)")
    else:
        # Not fatal. The verify endpoints are a convenience; the KV call below is the
        # thing this token actually has to do, so let that be the verdict.
        print("  Verify  : inconclusive - checking what it can actually do instead")

    try:
        d = call(f"https://api.cloudflare.com/client/v4/accounts/{acct}"
                 f"/storage/kv/namespaces/{ns}")
        title = (d.get("result") or {}).get("title", "?")
        print(f"  KV      : namespace reachable - \"{title}\"")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        if e.code in (401, 403):
            print(f"  KV      : HTTP {e.code} - this token cannot reach the namespace.")
            print("            Permission must be Workers KV Storage - Write (not Read),")
            print("            on the account that owns this namespace.")
        elif e.code == 404:
            print(f"  KV      : HTTP 404 - no namespace {ns} in account {acct}.")
            print("            One of those two ids is wrong.")
        else:
            print(f"  KV      : HTTP {e.code} - {body}")
        return 1
    except Exception as e:
        print(f"  KV      : could not check - {e}")
        return 1

    print(f"  Worker  : {CONFIG['WORKER_URL']}")
    print("\n  All good. Run without --check to start the tunnel.")
    return 0


def main():
    want_check = "--check" in sys.argv
    if "PASTE" in CONFIG["API_TOKEN"] or not CONFIG["API_TOKEN"].strip():
        sys.exit(
            "No Cloudflare API token, so the Worker cannot be pointed at the tunnel.\n"
            f"Create this file:  {SECRETS}\n"
            "with one line in it:\n"
            "    CF_API_TOKEN=cfat_your-token-here      (or cfut_ for a user token)\n"
            "Then run this script again — no need to reopen the terminal.\n"
            "(The file is in .gitignore, so the token stays out of git.)")
    if want_check:
        return check()
    print("Starting tunnel...")
    p = subprocess.Popen(
        [CONFIG["CLOUDFLARED"], "tunnel", "--url", CONFIG["LOCAL"],
         "--protocol", CONFIG["PROTOCOL"]],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    current = None
    try:
        for line in p.stdout:
            print(line, end="")
            m = URL_RE.search(line)
            # cloudflared prints the tunnel address in its own request and error logs
            # too, so a bare match fires dozens of times a day. Only a genuinely new
            # address is worth an API call.
            if m and m.group(0) != current:
                current = m.group(0)
                set_kv(current)
                print(f"\n  Fixed URL for phones: {CONFIG['WORKER_URL']}\n")
    except KeyboardInterrupt:
        print("\nStopping tunnel...")
    finally:
        # never leave cloudflared running without its parent
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
    return p.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
