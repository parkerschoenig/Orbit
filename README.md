# Orbit

Automation scripts for provisioning new VMs and LXC containers in a Proxmox homelab.

> **Note:** This is built specifically for my own setup (Proxmox + NetBox + AdGuard Home + NPMplus), but the approach is general enough that others may find it useful or adaptable. Config values are all externalized in `config.yaml` so it should be straightforward to point at your own services.

---

## Overview

Provisioning is split into two scripts that bracket the manual step of actually creating the VM/LXC in Proxmox, plus a third script for keeping SSH access in sync on hosts that already exist:

```
pre-deploy.py  →  [create the VM/LXC manually in Proxmox]  →  post-deploy.py
```

**`pre-deploy.py`** handles everything before the container exists — picking an IP, registering it in DNS/proxy/IPAM, and printing the exact settings to paste into the Proxmox UI.

**`post-deploy.py`** handles everything after — deploying SSH keys and hardening `sshd` via Ansible once the VM/LXC is up.

**`sync-ssh-keys.py`** — run any time you add a new key (e.g. a new device or service like Termix) and need it pushed out to hosts that were already deployed.

---

## Prerequisites

- Python 3.11+
- Ansible (for `post-deploy.py`)
- The following services reachable from the machine running these scripts:
  - **NetBox** — with IPAM configured (see [NetBox IPAM note](#netbox-ipam) below)
  - **AdGuard Home** — for internal DNS rewrites
  - **NPMplus** (Nginx Proxy Manager Plus) — for reverse proxy entries
  - **Proxmox** — you create the VM/LXC manually using the settings the script outputs

---

## Setup

```bash
git clone https://github.com/parkerschoenig/Orbit.git
cd Orbit
pip install -r requirements.txt
cp config.example.yaml config.yaml
# Edit config.yaml with your credentials and hostnames
```

---

## Configuration

`config.yaml` (gitignored) holds all credentials and defaults. See `config.example.yaml` for the full structure. Key sections:

| Section | Purpose |
|---|---|
| `proxmox.hosts` | List of Proxmox node FQDNs shown in the selector at runtime |
| `netbox` | URL + API token |
| `adguard` | URL, credentials, `dns_target` (where rewrites point), `dns_server` (shown to you as the DNS to set in new VMs) |
| `npmplus` | URL, credentials, `ssl_certificate` to auto-attach to new proxy hosts |
| `ssh_keys` | List of public key paths to deploy via `post-deploy.py` and `sync-ssh-keys.py` |
| `hosts` | List of existing VM/LXC hostnames or IPs for `sync-ssh-keys.py` to check/update |
| `defaults` | Default CPU/RAM/disk values pre-filled in prompts |

---

## pre-deploy.py

Run this **before** creating the VM/LXC in Proxmox.

```bash
python3 pre-deploy.py
# or to preview without making any changes:
python3 pre-deploy.py --dry-run
```

### What it does

1. **Proxmox node** — Select the target Proxmox host from the configured list (or type one if none are configured).
2. **FQDN** — Enter the fully-qualified domain name for the new VM/LXC (e.g. `myapp.lab.home`).
3. **IP selection** — Fetches active prefixes from NetBox, lets you pick a subnet, then suggests the next available IP. Pings the suggested IP to verify it's actually free. You can accept or enter your own.
4. **Hardware specs** — CPU cores, RAM (MB), and disk size (GB). Pre-filled from `defaults` in config.
5. **NPMplus proxy** — Optionally creates a reverse proxy entry. If `ssl_certificate` is set in config, the matching cert is looked up and attached automatically.
6. **Confirmation** — Shows a summary and asks before making any changes.
7. **Registration** — Creates:
   - AdGuard DNS rewrite: `fqdn → dns_target`
   - NPMplus proxy host (with SSL cert if configured)
   - NetBox VM entry with the chosen IP assigned and set as primary, parented to the selected Proxmox node
8. **Output** — Prints the hostname, IP, gateway, and DNS settings to paste into Proxmox when creating the VM/LXC.

---

## post-deploy.py

Run this **after** the VM/LXC is up and reachable via SSH.

```bash
python3 post-deploy.py 192.168.1.50
# or omit the IP to be prompted
python3 post-deploy.py
```

### What it does

1. Prompts for the root password (used only for this initial Ansible connection).
2. Prompts for `PermitRootLogin` preference (`prohibit-password` / `yes` / `no`).
3. Deploys all public keys from `ssh_keys` in config to `/root/.ssh/authorized_keys`.
4. Sets `PasswordAuthentication no` in `/etc/ssh/sshd_config`.
5. Clears any drop-in configs that re-enable password auth (common in some LXC templates).
6. Restarts `sshd`.

After this runs, the VM/LXC is only accessible via key auth.

---

## sync-ssh-keys.py

Run this any time you add a new key to `ssh_keys` in `config.yaml` (e.g. a new
device, or a service like Termix that needs SSH access to your hosts) and want
it pushed out to hosts that were already deployed.

```bash
python3 sync-ssh-keys.py                # sync every host in config.yaml
python3 sync-ssh-keys.py 192.168.1.50   # sync a single host
python3 sync-ssh-keys.py --dry-run      # show what would change, without applying it
```

### What it does

1. Connects to each host in `hosts` (config.yaml) using your current SSH
   identity — no password prompt, so hosts must already trust one of your
   existing keys (e.g. your personal PC or overlord key).
2. For each key in `ssh_keys`, checks whether that exact line already exists
   in `/root/.ssh/authorized_keys`.
3. Appends any missing keys. Keys already present, and any keys not listed in
   config, are left untouched.

This is additive-only — it never removes or overwrites existing
`authorized_keys` entries, unlike `post-deploy.py`, which replaces the file
wholesale on a freshly created VM/LXC.

---

## NetBox IPAM

For IP selection to work automatically, NetBox needs active **Prefixes** configured under IPAM. At minimum:

- Go to **IPAM → Prefixes** and create prefixes for your subnets (e.g. `192.168.10.0/24`).
- Set their status to **Active**.
- Optionally assign VLANs and descriptions — these are shown in the subnet selector.

Without prefixes, the script will error out at the IP selection step. You can still enter an IP manually at that point, but the automatic suggestion won't work.

NetBox also needs your Proxmox nodes added as **Devices** (under DCIM) — the selected node is set as the parent device on each new VM entry.
