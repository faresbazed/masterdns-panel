# MasterDNS Panel

A tiny, dependency-free web panel for [MasterDnsVPN](https://github.com/masterking32/MasterDnsVPN) — the DNS-tunnelling VPN that carries TCP traffic inside DNS queries.

The panel runs on your VPS next to the MasterDnsVPN server and lets you:

- **Create users.** Each user gets their *own tunnel subdomain* (`ahmed.example.com`). One MasterDnsVPN server serves all of them — the panel rewrites the server's `DOMAIN` list and restarts the service whenever users change. Disabling or deleting a user removes their domain from the server, so **their link stops working immediately** (that's the revocation: upstream MasterDnsVPN has one shared encryption key per server, so users are separated by domain, not by key).
- **Copy-paste client links** for every user, in all the formats the apps understand (see below).
- **Choose the DNS resolvers per user** — presets (Google, Cloudflare, Quad9, OpenDNS, AdGuard, Shecan) and/or any custom IPs, e.g. your ISP's resolver. The chosen resolvers are baked into every link the panel generates, so the client app connects through exactly the DNS you picked.
- A **per-user share page + subscription URL** you can send to the user — no admin login needed on their side.

The panel itself is a single Python file using only the standard library — no pip installs, no database server.

---

## 1. Before you install: DNS records on Cloudflare

You need a domain added to Cloudflare. Say your domain is `example.com` and your VPS IP is `203.0.113.10`:

1. In Cloudflare → your domain → **DNS → Records**, add the nameserver record:

   | Type | Name | Content | Proxy |
   |------|------|---------|-------|
   | A | `ns` | `203.0.113.10` (your VPS IP) | **DNS only** (grey cloud — never proxy this!) |

2. That's all you need *before* installing. The per-user records come after (step 3 below) — for each user the panel creates, add one NS delegation:

   | Type | Name | Target | Proxy |
   |------|------|--------|-------|
   | NS | `ahmed` (the user's label) | `ns.example.com` | DNS only |

   The panel's home page lists the exact records for all your users — just copy them into Cloudflare. Wait a few minutes after adding an NS record before connecting (DNS propagation).

> Why DNS-only? Cloudflare's proxy does not forward raw DNS/UDP 53 traffic to your VPS. The records must resolve straight to your server.

## 2. Install on the VPS

Fresh Debian/Ubuntu VPS (RHEL-family works too), as root:

```bash
git clone https://github.com/faresbazed/masterdns-panel.git
cd masterdns-panel
sudo bash install.sh
```

The installer asks for:

- **Base domain** — the domain that's on Cloudflare (e.g. `example.com`)
- **Nameserver host** — default `ns.example.com`
- **Admin password** for the panel (or it generates one and prints it once)

…and then it does everything else itself:

- installs requirements (`python3`, `curl`, `unzip`)
- downloads the **official MasterDnsVPN server** binary from upstream releases
- frees port 53 (disables the `systemd-resolved` stub listener, backs up its config; stops other DNS daemons if any are squatting on 53)
- generates the encryption key (AES-256-GCM by default) and the server config
- creates and starts two systemd services: `masterdnsvpn` and `masterdns-panel`
- **opens the firewall**: `53/udp`, `53/tcp`, and the panel port (`8080/tcp`)

> If your VPS provider has its **own cloud firewall** (Hetzner Cloud Firewall, AWS security groups, …), open the same ports there too — the installer can only configure the firewall inside the VPS.

Non-interactive install:

```bash
MDV_BASE_DOMAIN=example.com MDV_NS_HOST=ns.example.com \
MDV_ADMIN_PASSWORD='choose-a-strong-password' sudo -E bash install.sh
```

Useful env vars: `MDV_PANEL_PORT` (default `8080`), `MDV_ENCRYPTION_METHOD` (default `5` = AES-256-GCM; upstream ids: 0 none, 1 xor, 2 chacha20, 3 aes-128-gcm, 4 aes-192-gcm, 5 aes-256-gcm).

Open the panel at `http://YOUR_VPS_IP:8080` and sign in.

**Terminal menu:** the repo ships a small management menu — handy for restarts and removal without remembering commands:

```bash
sudo bash menu.sh
```

It offers: **1)** Install / reinstall, **2)** Restart services, **3)** Service status, **4)** Show panel info (URL/port, base domain, nameserver, user count), **5)** Uninstall (typed confirmation), **0)** Exit. The installer also copies it to `/opt/masterdns-panel/menu.sh` on the server, so it works there too.

## 3. Create a user and connect

1. In the panel, **Create user**: name, optional expiry, and the **resolvers** that user should connect through (presets and/or custom — this is where an ISP resolver for free-data DNS goes).
2. Add the user's **NS record** in Cloudflare (the panel's home page shows it, e.g. `ahmed.example.com  NS  ns.example.com`).
3. Copy the user's link and import it in the client app:

   | App | How |
   |-----|-----|
   | **V2Box / V2Box Pro** (iOS, Android, Mac) | Copy the `masterdns://` link → open V2Box → it offers to import from clipboard (or ＋ → Import from clipboard). Or use the **Subscription URL**. |
   | **Hiddify** (iOS, Android, desktop) | Copy the `masterdns://` link (either form — the panel offers the short and the long JSON form) → ＋ → *Add from clipboard*. Or add the **Subscription URL** as a subscription. |
   | **Original MasterDnsVPN client** (Windows/Linux/macOS/Termux) | Download the user's **client config .zip** from the panel, unzip it next to the client binary (`client_config.toml` + `client_resolvers.txt`), run the client, and point your apps at its SOCKS5 proxy `127.0.0.1:18000`. |
   | **MahsaNG** (Android) | Paste the `masterdns://` link as a new config. |

   If an app asks for fields one by one instead of a link, the user's share page lists them separately: tunnel domain, encryption key, encryption method, and resolvers.

4. The user can also open their **share page** (`http://YOUR_VPS_IP:8080/u/<token>`) themselves to copy the link — no admin password needed.

### Which resolvers should a user pick?

The client sends its DNS-tunnel queries *to the resolvers*; the resolvers forward them to your server. So:

- On a normal network, the default mix (Google + Cloudflare + Quad9 + OpenDNS) is a good choice — more resolvers = more paths survive blocking/loss.
- On a restricted network, pick resolvers that actually answer *from that network*. Well-known ones are often throttled first.
- For **free-data DNS** (ISP lets DNS through with no data balance, SlowDNS-style): use **only your ISP's own resolver IP** as a custom resolver — if the ISP blocks direct queries to public resolvers, the tunnel has to ride the ISP resolver. This only works if that resolver recursively forwards your tunnel domain (test: with no balance, can you resolve random websites' DNS? then you're fine).

## 4. Managing users

- **Disable** — the user's domain is removed from the server and their subscription/share links return 404. Re-enable any time.
- **Delete** — same, permanently.
- **Expiry date** — users expire the day after their expiry date and are dropped automatically from the server config within about a minute while the panel is running; their subscription stops working on expiry.
- **Change resolvers** — edit on the user's card; the links update instantly (resolvers are client-side, no server restart needed).
- **Encryption method** (Server settings) — changing it generates a *new key* and re-issues every link; all users must re-import.

## 5. Ports

| Port | Proto | What |
|------|-------|------|
| 53 | UDP + TCP | MasterDnsVPN server (the tunnel itself) |
| 8080 | TCP | This panel (change with `MDV_PANEL_PORT`) |
| 22 | TCP | Your SSH (installer leaves it allowed in ufw) |

The panel is plain HTTP. If you expose it publicly, prefer putting it behind a reverse proxy with HTTPS, or only open the panel port to your own IP in your provider's firewall.

## 6. Tests

```bash
python3 tests/test_panel.py   # panel behaviour over HTTP with throwaway data (no root/network)
python3 tests/e2e_local.py    # REAL upstream server+client binaries + panel-generated configs,
                              # pushes real traffic through the tunnel to a local HTTP target.
                              # Run it on a normal machine/VPS — some sandboxes block UDP replies.
```

## 7. Uninstall

Three ways, all running the same `uninstall.sh` (which keeps a backup of users/settings under `/root/`):

```bash
sudo bash uninstall.sh   # from the repo checkout
```

- **Terminal menu:** `sudo bash menu.sh` → option **5** (asks you to type `UNINSTALL`).
- **From the web panel:** at the bottom of the admin page, the **Danger zone — uninstall** section has an *Uninstall panel & server* button. Type `UNINSTALL` to confirm; the panel then launches the uninstaller in the background (the installer copies `uninstall.sh` next to `app.py` for exactly this) and the panel goes offline a few seconds later.

## How it works / credits

- All tunnelling is done by the upstream [MasterDnsVPN](https://github.com/masterking32/MasterDnsVPN) (MIT) server and clients — this repo is only the management panel, its installer, and the link/config generation. The server config template in `panel/server_config.template.toml` is adapted from upstream's sample config.
- Client link formats follow Hiddify's ray2sing converter (`masterdns://` short + JSON forms, `dnst://` form) and the hiddify-sing-box `masterdns` outbound spec — the generated links were verified by parsing them with ray2sing itself and by booting the real upstream server + client from panel-generated configs.
- One honest limitation: upstream MasterDnsVPN uses **one encryption key per server**, shared by all clients. The panel isolates users by tunnel subdomain (which is enforced by the server's domain matcher — a removed domain gets no service), not by separate keys. If you need per-user keys, run one panel/server pair per trust group.
