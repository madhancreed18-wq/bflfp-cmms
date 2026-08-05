# Fixed URL with a named Cloudflare Tunnel (free tunnel, ~$10/yr domain)

Result: **https://cmms.YOURDOMAIN.com** — permanent, HTTPS, push works,
survives reboots, no router/port changes.

## One-time setup (~30 min)

1. **Cloudflare account** (free): https://dash.cloudflare.com/sign-up

2. **Get a domain** — either:
   - Buy one inside Cloudflare (Domain Registration → Register, at-cost ~$10/yr), or
   - Ask IT to add a company domain to Cloudflare (bigger decision)

3. **Install cloudflared** (on the notebook / plant server):
   Download `cloudflared-windows-amd64.exe` from
   https://github.com/cloudflare/cloudflared/releases
   Rename to `cloudflared.exe`, put in `C:\cloudflared\`

4. **Login + create the tunnel** (Command Prompt):
   ```
   cd C:\cloudflared
   cloudflared tunnel login
   cloudflared tunnel create bflfp
   cloudflared tunnel route dns bflfp cmms.YOURDOMAIN.com
   ```
   The `create` step prints a credentials file path like
   `C:\Users\<you>\.cloudflared\<TUNNEL-ID>.json` — note the ID.

5. **Config file** — save as `C:\Users\<you>\.cloudflared\config.yml`:
   ```yaml
   tunnel: <TUNNEL-ID>
   credentials-file: C:\Users\<you>\.cloudflared\<TUNNEL-ID>.json
   ingress:
     - hostname: cmms.YOURDOMAIN.com
       service: http://localhost:8000
     - service: http_status:404
   ```

6. **Run as a Windows service** (auto-start on boot):
   ```
   cloudflared service install
   ```

Done. Open https://cmms.YOURDOMAIN.com on any phone → login → allow
notifications. The URL never changes again.

## Optional: lock it to your team (free, recommended)

Cloudflare dashboard → Zero Trust → Access → Applications → Add:
protect `cmms.YOURDOMAIN.com`, policy = allow emails
`@bluefalo-group.com` (up to 50 users free). Outsiders see a login wall.

## Later: move to the plant server

Repeat steps 3–6 on the server (same tunnel or a new one) — the public
URL stays the same; nothing changes on the phones.
