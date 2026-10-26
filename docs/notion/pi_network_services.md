# Pi Network Services — Tailscale & Cloudflare (setup, config, recovery)

**Category:** Infrastructure
**When to use:** setting up remote access on a fresh/rebuilt Pi, or when Tailscale (admin/VPN) or the Cloudflare Tunnel (public dashboard access) needs reinstalling or reconfiguring.

This is the **capabilities + configuration reference** for the two network services. For the *"it broke, get connectivity back"* troubleshooting flow (forwarding, exit-node approval, DNS), see [Restoring VPN / Remote Access](pi_vpn_restore.md) — this page is the standing setup/recovery of the services themselves.

The Pi runs two independent, complementary services:

| Service | Provides | Reaches |
|---|---|---|
| **Tailscale** (`tailscaled`) | Admin SSH onto the Pi + **exit node** (routes client internet through the Pi) | Private — only devices on the `trybooking.co.uk` tailnet |
| **cloudflared** (`cloudflared`) | **Cloudflare Tunnel** — publishes the internal dashboard to a public hostname over TLS, no inbound firewall ports | Public — anyone with the URL + Cloudflare Access auth |

Both are checked nightly by `scripts/health_check.py` (`systemctl is-active` + `tailscale status`), so a dead service emails an alert.

---

## Tailscale

### Capability
- **Tailscale SSH** — access is `ssh root@trybookingpi.tail1fb257.ts.net` (MagicDNS) or the tailnet IP `100.79.35.128`. Auth is by Tailscale identity, no local SSH keys.
- **Exit node** — the Pi advertises itself so tailnet clients can route all internet traffic through it.
- Node identity: hostname `TrybookingPi`, tag `tag:server`, tailnet `trybooking.co.uk`.

### Dependencies
- The `tailscale` package (daemon `tailscaled` + `tailscale` CLI).
- **IP forwarding** enabled for the exit node: `/etc/sysctl.d/99-tailscale.conf` with
  `net.ipv4.ip_forward = 1` and `net.ipv6.conf.all.forwarding = 1`.

### Config / state files
| Path | What it is |
|---|---|
| `/var/lib/tailscale/tailscaled.state` | **Node identity / keys** — if this survives, `tailscale up` reconnects without re-auth |
| `/etc/default/tailscaled` | daemon flags (port etc.) |
| systemd unit `tailscaled.service` | the daemon |
| `/etc/sysctl.d/99-tailscale.conf` | persisted IP forwarding (exit node) |
| Tailnet **ACLs** (admin console, not on the Pi) | the `tag:server` + `ssh` rules that grant access; the exit-node **approval** |

### Install / recovery
```bash
# 1. Install
curl -fsSL https://tailscale.com/install.sh | sh

# 2. Persist forwarding (exit node)
printf 'net.ipv4.ip_forward = 1\nnet.ipv6.conf.all.forwarding = 1\n' | sudo tee /etc/sysctl.d/99-tailscale.conf
sudo sysctl -p /etc/sysctl.d/99-tailscale.conf

# 3. Bring the node up with SSH + exit node advertising
sudo tailscale up --ssh --advertise-exit-node --hostname=TrybookingPi
#    -> open the printed URL, sign into the trybooking.co.uk tailnet, approve.
```
In the **admin console** afterwards: re-apply `tag:server` to the node, confirm the `ssh` ACL rule targets `tag:server` with `users:["root"]`, and **approve the exit node** (Machines → TrybookingPi → Edit route settings → green tick). Exit-node advertising alone is not enough — it must be approved.

### Verify
```bash
systemctl is-active tailscaled
tailscale status                # Pi should show "offers exit node"; peers listed
tailscale status --json | grep -i backendstate    # "Running"
```

---

## Cloudflare Tunnel (cloudflared)

### Capability
Exposes the internal dashboard (served locally by **Caddy** — which terminates HTTPS using the certs in `/root/certs/` — in front of the SvelteKit app on `:3000`) to a **public Cloudflare hostname over TLS**, without opening any inbound ports on the router/firewall. Access can be gated by **Cloudflare Access** policies in the Zero Trust dashboard.

### This Pi runs a **dashboard-managed (token) tunnel**
Confirmed from `systemctl cat cloudflared`:
```
ExecStart=/usr/bin/cloudflared --no-autoupdate tunnel run --token eyJ…
```
That means: **there is no local `config.yml` and no `/root/.cloudflared/` credentials — and that's correct.** The tunnel's ingress rules and public hostname(s) live entirely in the **Cloudflare Zero Trust dashboard** (Networks → Tunnels → *your tunnel* → **Public Hostname**). The **connector token** embedded in the systemd unit is the only local secret. Do **not** commit that token to git.

### Dependencies
- `cloudflared` (installed here from `/root/cloudflared-linux-arm64.deb`; currently v2026.5.1), running as the `cloudflared` systemd service.
- A Cloudflare account with the domain + the tunnel already created in **Zero Trust → Networks → Tunnels**.
- Outbound HTTPS/QUIC (443) from the Pi — the tunnel dials *out*, so no inbound firewall rules.

### Config / credential locations (token mode)
| Location | What it is |
|---|---|
| `/etc/systemd/system/cloudflared.service` | the connector; `ExecStart … --token <TOKEN>` is the credential |
| Cloudflare **Zero Trust dashboard** | the actual config: public hostname → `service` (the local dashboard), ingress rules, Access policies. **Not on the Pi.** |
| `/etc/cloudflared/` | empty in token mode (no config.yml — expected) |
| `/root/certs/trybooking.internal.pem` + `…-key.pem` | **Caddy's** internal TLS cert/key (HTTPS to the dashboard), *not* cloudflared. Keep `600`; back up alongside secrets. |

### Install / recovery (token mode)
On a rebuild, reconnect the existing tunnel with its token — get it from **Zero Trust → Networks → Tunnels → *your tunnel* → Configure → Install connector** (choose Debian/arm64; the command shown embeds the token). Or read the current one from `systemctl cat cloudflared` before wiping.
```bash
sudo dpkg -i /root/cloudflared-linux-arm64.deb          # or re-download the .deb
sudo cloudflared service install <CONNECTOR-TOKEN>       # writes the systemd unit with the token
sudo systemctl enable --now cloudflared
```
No `cloudflared tunnel login`, `cert.pem`, or `config.yml` is needed for a token connector — the tunnel definition already exists in the dashboard. If the public hostname ever stops resolving to the dashboard, fix the **Public Hostname** mapping in the dashboard (→ `http://localhost:80`, the Caddy front end), not on the Pi.

> Also restore `/root/certs/trybooking.internal*.pem` (Caddy's HTTPS cert) on a rebuild, or Caddy can't serve the dashboard for the tunnel to reach.

### Keeping cloudflared patched
The connector runs with `--no-autoupdate`, so **security releases have to be applied by hand** — cloudflared will not update itself. Periodically (and whenever Cloudflare emails a security advisory) check the running version against the latest release:
```bash
cloudflared --version                                   # what the Pi is running
# compare to https://github.com/cloudflare/cloudflared/releases/latest
```
Upgrade in place (ARM64 .deb — leaves the token and systemd unit untouched):
```bash
cd /root
curl -L -o cloudflared-linux-arm64.deb \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
sudo dpkg -i cloudflared-linux-arm64.deb
sudo systemctl restart cloudflared                      # ~30s tunnel blip; config/token unchanged
```
A single-connector restart is a ~30-second outage on the dashboard — fine for an internal tool.

**On a CA/certificate advisory** (e.g. the Oct 2026 eMudhra weak-key issue, fixed in cloudflared **2026.10.2**): the origin-rejection caveat only bites if the tunnel's *origin* is an HTTPS endpoint whose cert chains to an affected CA. Here the origin is local **Caddy** with the internal `trybooking.internal` cert (not a public CA), so the tunnel keeps working after the upgrade — but patch anyway to clear the client-side vulnerability. **Never** paper over a cert error with `noTLSVerify`; fix the cert instead.

### Verify
```bash
systemctl is-active cloudflared
journalctl -u cloudflared -n 30 --no-pager     # look for "Registered tunnel connection"
cloudflared tunnel list                          # the tunnel shows healthy connections
```

---

## Recovery order on a fresh Pi
1. **Tailscale first** — it's your remote hands (`ssh root@trybookingpi.tail1fb257.ts.net`). Without it you need physical/console access.
2. **cloudflared** — restore the tunnel so the dashboard is reachable publicly again.
3. Then the app/pipeline recovery in [pi_vpn_restore.md](pi_vpn_restore.md).

## Good to know
- **Two different jobs**: Tailscale = private admin + exit-node routing; cloudflared = public dashboard. A failure in one doesn't affect the other.
- The pieces that let a rebuild reconnect: Tailscale's node identity (`/var/lib/tailscale/tailscaled.state`) and cloudflared's **connector token** (in the systemd unit; re-obtainable from the Zero Trust dashboard). Neither is in git — a wiped SD card means re-authing Tailscale in the browser and re-installing the cloudflared token from the dashboard. Also restore `/root/certs/trybooking.internal*.pem` (Caddy's HTTPS cert).
- The nightly `health_check.py` alerts if either service is down; `tailscale status` not "offering exit node" is flagged as a WARN (clients would lose internet routing).
- After any reset, re-check IP forwarding (`/etc/sysctl.d/99-tailscale.conf`) and the exit-node **approval** — the two things a reset most often knocks out (see pi_vpn_restore.md, Fixes 1–2).
