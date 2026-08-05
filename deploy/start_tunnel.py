"""Start a Cloudflare quick tunnel and point the Worker at it automatically.

Run this INSTEAD of running cloudflared by hand:
    python deploy/start_tunnel.py

It launches cloudflared, waits for the random trycloudflare URL, then updates
the Worker's KV key so your fixed workers.dev URL forwards to it.

One-time setup (fill CONFIG below):
1. Cloudflare dashboard -> Workers & Pages -> KV -> Create namespace "TUNNEL".
   Open your Worker -> Settings -> Bindings -> Add -> KV namespace,
   variable name TUNNEL, select the namespace. Note the namespace ID.
2. My Profile -> API Tokens -> Create Token -> Custom:
   Permission: Account / Workers KV Storage / Edit. Copy the token.
3. Account ID: dashboard right sidebar (Workers & Pages overview).
"""
import re, subprocess, sys, urllib.request, json, os

CONFIG = {
    "ACCOUNT_ID": "PASTE_ACCOUNT_ID",
    "KV_NAMESPACE_ID": "PASTE_KV_NAMESPACE_ID",
    "API_TOKEN": "PASTE_API_TOKEN",
    "CLOUDFLARED": r"C:\cloudflared\cloudflared.exe",
    "LOCAL": "http://localhost:8000",
}

def set_kv(url):
    api = (f"https://api.cloudflare.com/client/v4/accounts/{CONFIG['ACCOUNT_ID']}"
           f"/storage/kv/namespaces/{CONFIG['KV_NAMESPACE_ID']}/values/target")
    req = urllib.request.Request(api, data=url.encode(), method="PUT",
        headers={"Authorization": f"Bearer {CONFIG['API_TOKEN']}",
                 "Content-Type": "text/plain"})
    with urllib.request.urlopen(req) as r:
        ok = json.loads(r.read()).get("success")
    print(f"  Worker target updated -> {url}" if ok else "  ! KV update failed")

def main():
    if "PASTE" in CONFIG["API_TOKEN"]:
        sys.exit("Fill in CONFIG at the top of this file first (see docstring).")
    print("Starting tunnel...")
    p = subprocess.Popen([CONFIG["CLOUDFLARED"], "tunnel", "--url", CONFIG["LOCAL"]],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in p.stdout:
        print(line, end="")
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            set_kv(m.group(0))
            print("\n  Fixed URL for phones: https://YOUR-WORKER.workers.dev\n")
    p.wait()

if __name__ == "__main__":
    main()
