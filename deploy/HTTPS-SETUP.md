# HTTPS for phones — BFLFP CMMS (Caddy)

Phones only allow **"install as an app"** and **push notifications** over HTTPS.
On plain `http://<PC-IP>:8000` the browser blocks both. This guide puts Caddy in
front of the app so phones reach it over `https://<PC-IP>` with a locally-trusted
certificate — fully self-hosted, nothing leaves the building.

End result: the app runs on port 8000, Caddy serves HTTPS in front of it, and each
phone (after a one-time certificate trust) can install the app and receive push.

---

## Prerequisites

- The plant PC/notebook and all phones are on the **same wifi**.
- You have **Administrator** rights on the PC (Caddy binds port 443 and installs a
  local certificate authority).

---

## Step 1 — Install Caddy on the PC (once)

Pick whichever is easiest:

- **Scoop:** `scoop install caddy`
- **Chocolatey:** `choco install caddy`
- **Manual:** download `caddy_windows_amd64.exe` from
  https://github.com/caddyserver/caddy/releases → rename it to `caddy.exe` →
  put it in the project root (next to `run.py`).

`start_caddy.py` finds Caddy on your PATH or a `caddy.exe` in the project root.

## Step 2 — Give the PC a fixed LAN IP (recommended)

Phones install the app pointing at a specific address, so that address should not
change. On your router, add a **DHCP reservation** for this PC (or set a static IP).
If the IP changes later, phones must reopen the new URL and re-install.

## Step 3 — Start the app, then Caddy

1. Double-click **Start CMMS.bat** — the app comes up on port 8000.
2. Double-click **Start HTTPS (Caddy).bat** (right-click → *Run as administrator*
   the first time). It prints the phone URL, e.g. `https://192.168.1.50`.
   - On first run Caddy installs its local CA into Windows — accept the prompt.
   - If Windows Firewall asks, **allow** Caddy on Private networks.

Leave both windows running. (Later you can run them as Windows services so they
auto-start on boot.)

## Step 4 — Trust Caddy's root certificate on each phone

This is the one manual step per phone. Copy the CA file from the PC to the phone:

```
%AppData%\Caddy\pki\authorities\local\root.crt
```

(Email it to yourself, use USB, or drop it temporarily in the `static/` folder and
download it from the phone.) Then:

### Android

Settings → **Security** → *(More security settings)* → **Encryption & credentials**
→ **Install a certificate** → **CA certificate** → pick `root.crt` → confirm.
You'll see a "network may be monitored" note — that's normal for a private CA.
*(The exact menu path varies a little by phone brand.)*

### iPhone / iPad

1. Open `root.crt` on the phone (AirDrop or email) → it says **Profile Downloaded**.
2. Settings → **General** → **VPN & Device Management** → tap the Caddy profile →
   **Install**.
3. **Important:** Settings → **General** → **About** → **Certificate Trust Settings**
   → turn **ON** full trust for the *Caddy Local Authority*. Safari will not trust
   it without this toggle.

## Step 5 — Install the app + turn on notifications (each phone)

1. Open **`https://<PC-IP>`** in **Chrome** (Android) or **Safari** (iOS).
   You should see a padlock / "secure".
2. Log in.
3. Install it:
   - **Android:** browser menu → *Install app*, or in the app go to
     **More (อื่นๆ) → 📲 Install app**.
   - **iPhone:** **Share** → **Add to Home Screen**.
4. Open the app from its new home-screen icon → **allow notifications** when asked.

---

## Verify push works

Have someone release a job to a technician, or report a **ด่วน (Critical)**
breakdown. The assigned tech's phone should get a notification **even with the app
closed**.

- **iOS note:** push requires **iOS 16.4+** and the app must be opened from the
  **home-screen icon** (the installed version), not a Safari tab.

## Troubleshooting

- **"Not secure" / certificate warning on a phone** → the CA isn't trusted on that
  phone yet. Redo Step 4 (on iPhone, don't forget the trust toggle in step 3).
- **Phone can't reach the site** → same wifi? Windows Firewall allowing port 443?
  Did the PC's IP change? (Use a DHCP reservation.)
- **No notifications** → permission granted? App opened at least once from the
  home-screen icon? Padlock present (real HTTPS)?
- **IP keeps changing** → set a DHCP reservation / static IP for the PC.

## Alternative: skip per-phone cert install (Cloudflare Tunnel)

If trusting the CA on many phones is too much hassle, a **Cloudflare Tunnel** gives a
real, publicly-trusted HTTPS URL — no certificate to install on any device, and it
also works **outside** the plant. See **deploy/CLOUDFLARE-TUNNEL.md**.
Trade-off: it needs a ~$10/yr domain and routes traffic through Cloudflare's edge
(lock it to `@bluefalo-group.com` with Zero Trust Access, which is free).
