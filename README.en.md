# CRDN — Server Monitor

**[Русский](README.md) | English**

**A lightweight Linux server monitoring dashboard, right in your browser.**

CRDN displays CPU usage, memory usage, disk usage and network activity in real time.

No Docker, external monitoring services or heavyweight databases.

[GitHub](https://github.com/GreyByDefault/CRDN-PUBLIC) · [Releases](https://github.com/GreyByDefault/CRDN-PUBLIC/releases)

## Current release

**v0.1.0 is a Pre-release for exploring the project and its source code.** The metrics collector, web dashboard and authentication already work. The universal installer will arrive in **v1.0.0**; installation, update, removal and access recovery scripts have not been released yet.

The instructions below describe the intended v1.0.0 installation. You can prepare DNS and connect over SSH now; sections about future scripts are clearly marked. Commands for these scripts are not available yet.

## What CRDN looks like

Four cards — CPU, RAM, Disk and Network — on a dark graphite background with turquoise accents. A 2 × 2 grid on desktop and a single column on phones.

### Features

- **CPU** — current, average, minimum and maximum CPU usage.
- **RAM** — total, used and available memory.
- **Disk** — total, used and free disk space.
- **Network** — incoming and outgoing rates and established TCP connection count.
- Metrics refresh every 2 seconds.
- Charts show the last 5 minutes of history.
- A convenient interface for computers and smartphones.
- Login with a username and password, which you can change in account settings.

CRDN is designed for observing your server. It does not manage system resources or change their settings.

---

## 1. What you need for installation

The intended v1.0.0 installation targets:

- A VPS/VDS, dedicated server or your own Linux server.
- Ubuntu Server 22.04, 24.04, 26.04 LTS or Debian 12/13 with systemd. This is the target list for the v1.0.0 installer; compatibility testing across all listed versions is still pending.
- Administrative SSH access to the server.
- A domain or subdomain.
- An internet connection on the server.

CRDN runs on Linux servers. Standard shared web hosting is not supported.

Installation does not require Docker, a separate database or a paid monitoring service.

## 2. Setting up your domain

**Before installing CRDN, point your domain to your server's IP address.**

Configure this in your domain's DNS management panel — for example, REG.RU, Cloudflare or another service that hosts its DNS.

You can find your server's IP address in the account dashboard of your VPS provider.

Two options are shown below. Choose the one that suits you.

### Option A. Install on the main domain

Suppose you bought this domain:

`example.com`

And want CRDN to open directly at:

`https://example.com`

Open your DNS settings and add this record:

| Field | Value |
|---|---|
| Record type | `A` |
| Name | `@` |
| Value | `203.0.113.10` |
| TTL | Default |

`@` means the main domain.

Some DNS providers use an empty field or the domain name itself instead of `@`. Follow the notation used in your provider's panel.

`203.0.113.10` is an example. Replace it with **your server's actual public IPv4 address**.

After you save the record, `example.com` will point to the specified server.

**Important:** if a website already runs on the main domain, installing CRDN at the same address will require changes to the existing configuration. A subdomain is more convenient for a separate dashboard.

### Option B. Install on a subdomain

Suppose your main website is at:

`https://example.com`

And you want to host CRDN separately at:

`https://monitor.example.com`

Open your DNS settings and add this record:

| Field | Value |
|---|---|
| Record type | `A` |
| Name | `monitor` |
| Value | `203.0.113.10` |
| TTL | Default |

Here, `monitor` is the subdomain name.

You can choose another name, such as `server`, `status` or `crdn`.

In the value field, enter your server's actual public IPv4 address.

After you save the record, your main website will continue to work at `example.com`, while `monitor.example.com` will point to the same server.

**We recommend a dedicated subdomain for CRDN.**

### How long does DNS take to update?

You can usually start checking after 10–15 minutes. In some cases, updates take several hours.

In v1.0.0, the CRDN installer will automatically check where your domain points.

If DNS has not updated yet or points to another server, installation will stop with a clear message. Once you fix DNS, you will be able to run the installation command again.

**This check belongs to the upcoming installer; it is not available in v0.1.0.**

## 3. Connecting to your server

To install CRDN, connect to your Linux server over SSH.

**Windows:** open PowerShell or Windows Terminal.

**Linux and macOS:** open Terminal.

Enter:

```bash
ssh root@203.0.113.10
```

Replace `203.0.113.10` with your server's IP address.

If your provider gave you a different username, use it instead of `root`. Installation will require administrator privileges through `sudo`.

On your first connection, SSH may ask you to confirm the server's host key. Compare its fingerprint with the information supplied by your provider.

Once connected, you can proceed to CRDN installation.

## 4. Installing CRDN — coming in a future release

**The universal installer will arrive in v1.0.0. There is no ready-to-use installation command in v0.1.0.** The first version is for exploring the project and its source code; local developer startup is described in section 9.

The planned v1.0.0 installer workflow:

1. Check the operating system and server resources.
2. Check for required programs and install missing dependencies.
3. Detect the installed web server: Caddy, Nginx or Apache.
4. Ask for the dashboard's domain or subdomain.
5. Check DNS and the availability of required network ports.
6. Download a stable CRDN version.
7. Install the application and configure automatic startup.
8. Connect the dashboard to the web server and configure HTTPS.
9. Verify the application and display the dashboard address.

**The v1.0.0 installer will only ask you for a domain or subdomain.** Everything else will be handled automatically.

### If your server already hosts websites

Support for Caddy, Nginx and Apache is planned for the v1.0.0 installer. A dedicated deployment configuration using Caddy is already implemented; automatic Nginx and Apache integration is not implemented yet.

The installer will detect the existing web server and add the dashboard configuration while preserving other websites.

If no web server is present, the installer will install and configure Caddy.

If a conflict cannot be resolved safely, installation will stop and explain why. Existing websites and services must remain unchanged.

## 5. Signing in to CRDN for the first time

Web login already works in v0.1.0. For a deployed dashboard, open its HTTPS address. In v1.0.0, the installer will display this address when installation finishes.

For example:

`https://monitor.example.com`

Initial credentials for a new installation without an existing account:

**Username:** `admin`

**Password:** `admin`

After you sign in, a notification will offer to change your username and password.

Choose **Change** (Изменить) to set your own credentials or **Cancel** (Отмена) to continue with the defaults.

You can change your username and password later under **Account** (Учётная запись), confirming your current password.

A session remains valid for 30 minutes after the user's last action. Automatic metric refreshes do not extend it. **Log out** (Выйти) ends the session immediately. Passwords are stored as strong hashes; the account survives application restarts and updates.

**We strongly recommend changing the default credentials immediately after installation.**

The `admin / admin` combination is publicly known. While you use it, anyone who knows the dashboard address can access it.

## 6. Updating CRDN — coming in a future release

New versions are published on [GitHub Releases](https://github.com/GreyByDefault/CRDN-PUBLIC/releases).

**The universal update command is being prepared for v1.0.0; it has not been released in v0.1.0.**

Planned behavior: updates will preserve:

- Your username and password.
- Your domain or subdomain.
- HTTPS settings.
- CRDN service configuration.
- Other user-defined application settings.

After an update, the dashboard should continue working at the same address without repeating the initial setup. Account persistence is already implemented; the universal update workflow still needs to be implemented.

### What types of versions are there?

- **Stable** — a stable version recommended for regular installation.
- **Beta** — a preliminary version with new features.
- **Alpha** — an early experimental version.

**v0.1.0 is a Pre-release; there is no stable version or universal installer yet.**

In v1.0.0, the regular installation command will use the latest stable CRDN version. Preliminary versions will be selected separately, at the user's explicit choice.

## 7. Removing CRDN — coming in a future release

**The removal script is being prepared for v1.0.0; v0.1.0 has no ready-to-use removal command.**

The planned script will stop the service, remove application files and remove the web server configuration created for CRDN.

Other websites, services and applications on the server must remain unchanged.

The script will ask for separate confirmation before deleting stored credentials.

## 8. Troubleshooting

### The domain does not open

Check the DNS A record. It must point to your server's public IP address.

If you just added the record, wait and try again.

### HTTPS does not work

Make sure DNS is configured correctly and the required network ports are accessible from the internet.

CRDN uses HTTPS for secure connections to the dashboard.

### A website already runs on the server

You do not need to remove your existing website or web server.

Automatic installation alongside existing projects and support for Caddy, Nginx and Apache are being prepared for v1.0.0. There is no universal installer in v0.1.0; the existing deployment configuration is intended for a dedicated Caddy site.

### The operating system is not supported

Check your Linux version. The target system list for the v1.0.0 installer is in the first section. Automatic compatibility checks have not been released yet.

### Forgotten username or password — recovery is coming in a future release

**A secure access recovery workflow is being prepared for v1.0.0. v0.1.0 has no ready-to-use recovery script or universal recovery command.** Email-based recovery is not planned.

## 9. Source code and license

CRDN is an open-source NEUROFLO project.

- [Source code on GitHub](https://github.com/GreyByDefault/CRDN-PUBLIC)
- [Official releases](https://github.com/GreyByDefault/CRDN-PUBLIC/releases)
- [MIT License](LICENSE).

### For developers: running v0.1.0 locally

You need Linux with readable `/proc`, Python 3 and a modern browser. No third-party Python packages, Node.js or build step are required. From the root of the source checkout:

```bash
python3 -m crdn.api
```

Open `http://127.0.0.1:8765/`. Stop with **Ctrl+C**. The application listens only on `127.0.0.1`; local startup does not configure systemd, DNS or HTTPS and does not replace an installer. Cookies use Secure/HttpOnly/SameSite flags; external access requires HTTPS.

History lives only in browser memory. Unavailable metrics appear as a dash, and the API returns `null`; an actual absence of TCP connections is displayed as `0`. `/api/metrics` requires login.

- [Contribution guidelines](CONTRIBUTING.md).

---

**CRDN Server Monitor — only the metrics you need. Nothing extra.**
