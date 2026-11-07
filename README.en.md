# CRDN — Server Monitor

**[Русский](README.md) | English**

A lightweight NEUROFLO Linux server dashboard: CPU, RAM, root disk and network. Python 3 standard library, HTML/CSS/JavaScript/SVG; no Docker, database or frontend build. MIT.

[Source](https://github.com/GreyByDefault/CRDN-PUBLIC) · [Releases](https://github.com/GreyByDefault/CRDN-PUBLIC/releases)

## Status

Published v0.1.0 remains a Pre-release without installation assets. Lifecycle scripts are implemented in source; stable v1.0.0 has not been released. Normal installation reports the absence of a stable release and never uses master or a Pre-release.

Target systems: Ubuntu Server 22.04/24.04/26.04 LTS, Debian 12/13 with systemd. **Full installation and ACME staging remain untested.** A real Debian 12 VM was used to check Caddy/Nginx/Apache configurations and discovery, occupied-domain refusal, candidate startup, updates preserving owner.json, rollback after failure and repeated installation of an already installed dashboard, and removal preserving credentials and an existing Nginx site. Locally, 15 tests pass (12 lifecycle and 3 existing release tests). Ubuntu and Debian 13 have not been tested; this does not establish readiness across the entire matrix.

## Features

- CPU: current usage, average, minimum and maximum over 5 minutes.
- RAM: total, used and available memory, percentage and chart.
- Disk: `/` only, total/used/free/available space and usage ring.
- Network: rates for one primary interface, charts and IPv4/IPv6 TCP connections.
- Approximately 2-second updates, browser-memory history only; unavailable values appear as a dash.
- Graphite dashboard, 2×2 on desktop and a single column on phones.
- Web login, account changes and logout. Server management is out of scope.

## Preparation

Root/sudo, Python 3, systemd, apt and standard system tools (`ip`, `ss`, `runuser`, `useradd`) are required. The installer checks their presence; missing baseline tools must be installed beforehand. Caddy and Certbot are installed through apt when needed. At least 1 GiB free in `/opt` and HTTPS access to GitHub and ACME are required.

Create DNS A and, when using IPv6, AAAA records for a dedicated domain/subdomain. **All** addresses must be public addresses assigned to this server. NAT, CDN and external proxy chains are not integrated automatically. Open inbound TCP 80/443 yourself; scripts do not change the firewall. Public HTTPS is checked after configuration; a local check cannot prove external port reachability, and certificate issuance depends on ACME.

Connect over SSH: `ssh root@SERVER_IP`. Verify the host key on first connection.

## Installation

Obtain the complete source directory from a trusted CRDN-PUBLIC tag, including `ops/`; a single script without its helpers will not work. Run these commands from that directory:

```sh
sudo sh install.sh
```

The script asks for a domain. For automation: `sudo sh install.sh --domain monitor.example.com`.

The application is downloaded from the latest stable GitHub Release: `crdn-vX.Y.Z.tar` and `crdn-vX.Y.Z.tar.sha256`. SHA-256 checks integrity; publisher trust relies on GitHub HTTPS, with no separate signature.

Before configuration changes, the installer examines services, processes on 80/443, effective configurations and domain occupancy. Standard package configurations of Caddy, Nginx and Apache are supported. Ambiguous setups, control panels, custom units, wildcard/regex conflicts and trusted upstream proxies require manual review. Apache must already have proxy, proxy_http, headers, ssl and alias modules enabled. The script does not enable them globally.

With no web server and free ports, Caddy is installed. Existing Caddy receives a separate site and import; Nginx/Apache receive a separate site file in the active include. The overall configuration is validated before reload. Caddy manages HTTPS itself; Nginx/Apache obtain a dedicated Certbot webroot certificate without configuration rewriting or server shutdown. Renewal uses a dedicated systemd timer and validates configuration before reload.

## First login

Open `https://YOUR_DOMAIN`. **admin / admin** is created only when no saved account exists. The browser offers an optional credential change; later, **Account** (Учётная запись) requires the current password. Change the default password promptly.

Sessions expire after 30 minutes of inactivity; metric polling does not extend them. The account survives updates and restarts; sessions require a new login after a restart. A separate forgotten-password recovery command is not implemented yet.

## Updating and removing

```sh
sudo sh update.sh
sudo sh uninstall.sh
```

Updates validate the archive, run a candidate with temporary data, switch code atomically and restore the previous release on failure. Username, password, domain and port are preserved. Repeating `install.sh` for an installed dashboard prints its address.

Removal deletes CRDN resources; deleting credentials requires a separate `DELETE` confirmation. By default they are retained for reinstallation. Packages, the shared web server and shared Caddy certificate storage are preserved. The system account remains to preserve data ownership. Administrator-modified files block automatic updates/removal until manually reconciled.

After interruption:

```sh
sudo sh install.sh --recover
```

The journal restores only its own changes when checksums match. Concurrent edits are preserved and require manual review. After recovery, remove an incomplete installation before reinstalling.

Code: `/opt/crdn-managed/releases`, atomic `current`; parameters, backups and journal: `/etc/crdn-managed`; account: `/var/lib/crdn-managed`. Service: `crdn-managed.service`, a dedicated unprivileged user and automatically selected loopback port. This is independent from the previously configured private deployment.

## Testing and release packaging

An explicit test source with the same runtime format, only on an isolated server with a test domain:

```sh
sudo sh install.sh --domain monitor.example.com --test-archive /tmp/crdn-v1.0.0.tar --test-sha256 YOUR_SHA256 --acme-staging
```

Staging issues an untrusted certificate. Do not use this flag for a production installation. Test sources are never selected automatically; a normal update of a staging site does not migrate its certificate to production.

From a clean checkout with committed runtime files:

```sh
python3 ops/build_release.py v1.0.0 /tmp/crdn-assets
```

This only builds the archive and SHA-256; it does not create a tag or release. For a future stable release, attach both files to the same CRDN-PUBLIC Release. The example version does not mean it has been published.

## Local startup

```sh
python3 -m crdn.api
```

Address: `http://127.0.0.1:8765/`; stop with Ctrl+C. Secure cookies require HTTPS for full browser login. Local startup does not configure HTTPS/systemd.

[Contributing](CONTRIBUTING.md) · [MIT](LICENSE)
