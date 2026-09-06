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
Exposes the internal dashboard (served locally by Caddy on `:80` → SvelteKit app on `:3000`) to a **public Cloudflare hostname over TLS**, without opening any inbound ports on the router/firewall. Access can be gated by **Cloudflare Access** policies in the Zero Trust dashboard.

### Dependencies
- The `cloudflared` binary/package (runs as the `cloudflared` systemd service).
- A Cloudflare account with the domain, and a **named tunnel** created in **Zero Trust → Networks → Tunnels**.
- Outbound HTTPS/QUIC (443) allowed from the Pi — the tunnel dials *out* to Cloudflare, so no inbound rules needed.

### Config / credential files
| Path | What it is |
|---|---|
| `/etc/cloudflared/config.yml` | tunnel config: `tunnel:` UUID, `credentials-file:`, and `ingress:` rules |
| `/root/.cloudflared/<TUNNEL-UUID>.json` | **tunnel credentials** (the secret that authenticates this connector) |
| `/root/.cloudflared/cert.pem` | origin cert from `cloudflared tunnel login` (needed to create/manage tunnels) |
| systemd unit `cloudflared.service` | the connector (`cloudflared service install`) |

A typical `config.yml` (ingress points at the local dashboard front end):
```yaml
tunnel: <TUNNEL-UUID>
credentials-file: /root/.cloudflared/<TUNNEL-UUID>.json

ingress:
  - hostname: <dashboard-public-hostname>
    service: http://localhost:80        # Caddy front end (or http://localhost:3000 direct)
  - service: http_status:404            # catch-all (required last rule)
```
> The real UUID, hostname and credentials live on the Pi and in the Cloudflare Zero Trust dashboard — they are **not** in git. Retrieve/recreate them from the dashboard on a rebuild.

### Install / recovery
**If the tunnel already exists** (rebuild, credentials lost) — reconnect it from the dashboard:
```bash
# install cloudflared (see developers.cloudflare.com for the current ARM64 package), then either:
#  A) token connector (simplest): Zero Trust -> Tunnels -> your tunnel -> Configure -> copy the token
sudo cloudflared service install <CONNECTOR-TOKEN>
#  B) config-file mode: restore /etc/cloudflared/config.yml + the <UUID>.json credentials, then
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```
**If recreating from scratch:**
```bash
cloudflared tunnel login                      # writes cert.pem
cloudflared tunnel create trybooking-dash     # writes <UUID>.json, prints the UUID
# write /etc/cloudflared/config.yml (see above), then route DNS:
cloudflared tunnel route dns trybooking-dash <dashboard-public-hostname>
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

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
- Tailscale node identity (`tailscaled.state`) and the cloudflared credentials (`<UUID>.json`, `cert.pem`) are the pieces that let a rebuild reconnect *without* re-auth — but they're on the SD card, not in git, so a wiped card means re-authing both from their dashboards.
- The nightly `health_check.py` alerts if either service is down; `tailscale status` not "offering exit node" is flagged as a WARN (clients would lose internet routing).
- After any reset, re-check IP forwarding (`/etc/sysctl.d/99-tailscale.conf`) and the exit-node **approval** — the two things a reset most often knocks out (see pi_vpn_restore.md, Fixes 1–2).
