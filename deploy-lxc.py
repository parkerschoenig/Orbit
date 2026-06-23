#!/usr/bin/env python3
"""
LXC Deployment Orchestrator
Automates: IP selection (NetBox) → LXC creation (Proxmox helper script) →
           DNS (AdGuard) → reverse proxy (NPMplus) → IPAM registration (NetBox)
           → SSH hardening (Ansible)
"""

import argparse
import ipaddress
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import questionary
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        console.print(
            f"[red]config.yaml not found.[/red] Copy config.example.yaml → config.yaml and fill in your values."
        )
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ── Helpers ───────────────────────────────────────────────────────────────────

def ip_from_cidr(cidr: str) -> str:
    """Strip prefix length: '192.168.20.10/24' → '192.168.20.10'"""
    return cidr.split("/")[0]


def gateway_from_cidr(cidr: str) -> str:
    """Derive gateway as first host in the network (common convention)."""
    net = ipaddress.ip_interface(cidr).network
    return str(next(net.hosts()))


def prefix_bits(cidr: str) -> str:
    """Return prefix length string: '192.168.20.10/24' → '24'"""
    return cidr.split("/")[1]


def validate_url(val: str) -> bool | str:
    if re.match(r"^https?://", val.strip()):
        return True
    return "Must start with http:// or https://"


def validate_fqdn(val: str) -> bool | str:
    val = val.strip()
    if re.match(r"^[a-z0-9]([a-z0-9\-\.]*[a-z0-9])?$", val, re.IGNORECASE) and "." in val:
        return True
    return "Enter a valid FQDN (e.g. myapp.lab.home)"


def validate_port(val: str) -> bool | str:
    try:
        p = int(val)
        if 1 <= p <= 65535:
            return True
    except ValueError:
        pass
    return "Enter a port number between 1 and 65535"


def validate_int(minimum: int = 1):
    def _v(val: str) -> bool | str:
        try:
            if int(val) >= minimum:
                return True
        except ValueError:
            pass
        return f"Enter an integer ≥ {minimum}"
    return _v


# ── Phase 1: Gather Metadata ──────────────────────────────────────────────────

def phase1_gather(cfg: dict) -> dict:
    console.print(Panel("[bold cyan]Phase 1 — Gather deployment info[/bold cyan]", expand=False))

    helper_url = questionary.text(
        "Proxmox helper script URL:",
        validate=validate_url,
    ).ask()
    if helper_url is None:
        sys.exit(0)
    helper_url = helper_url.strip()

    proxmox_node = questionary.text(
        "Proxmox node FQDN (parent host for this LXC):",
        default=cfg.get("proxmox", {}).get("host", ""),
        validate=validate_fqdn,
    ).ask()
    if proxmox_node is None:
        sys.exit(0)
    proxmox_node = proxmox_node.strip().lower()

    default_suffix = cfg.get("defaults", {}).get("domain_suffix", "")
    fqdn_hint = f"  (e.g. myapp.{default_suffix})" if default_suffix else ""
    fqdn = questionary.text(
        f"FQDN for the new LXC{fqdn_hint}:",
        validate=validate_fqdn,
    ).ask()
    if fqdn is None:
        sys.exit(0)
    fqdn = fqdn.strip().lower()
    hostname = fqdn.split(".")[0]

    return {
        "helper_url": helper_url,
        "proxmox_node": proxmox_node,
        "fqdn": fqdn,
        "hostname": hostname,
    }


def phase1_ip(cfg: dict) -> dict:
    from lib.netbox import NetBoxClient

    nb_cfg = cfg["netbox"]
    nb = NetBoxClient(nb_cfg["url"], nb_cfg["token"])

    console.print("\n[bold]Fetching subnets from NetBox…[/bold]")
    try:
        prefixes = nb.list_prefixes()
    except Exception as e:
        console.print(f"[red]NetBox error:[/red] {e}")
        sys.exit(1)

    if not prefixes:
        console.print("[red]No active prefixes found in NetBox.[/red]")
        sys.exit(1)

    # Build selection table
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("#", style="dim", width=4)
    table.add_column("Prefix")
    table.add_column("Description")
    table.add_column("VLAN")
    for i, p in enumerate(prefixes, 1):
        vlan = p.get("vlan") or {}
        vlan_label = f"{vlan.get('vid', '')} {vlan.get('name', '')}".strip() if vlan else ""
        table.add_row(str(i), p["prefix"], p.get("description") or "", vlan_label)
    console.print(table)

    choices = [f"{i}. {p['prefix']}  {p.get('description','')}" for i, p in enumerate(prefixes, 1)]
    selected = questionary.select("Which subnet/VLAN?", choices=choices).ask()
    if selected is None:
        sys.exit(0)
    prefix_index = int(selected.split(".")[0]) - 1
    chosen_prefix = prefixes[prefix_index]

    console.print(f"\n[bold]Finding next available IP in {chosen_prefix['prefix']}…[/bold]")
    try:
        suggested_cidr = nb.next_available_ip(chosen_prefix["id"])
    except Exception as e:
        console.print(f"[red]NetBox error:[/red] {e}")
        sys.exit(1)

    def ping_ip(ip: str) -> bool:
        """Return True if the IP responds to ping (already in use)."""
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "1", ip],
            capture_output=True,
        )
        return result.returncode == 0

    if suggested_cidr:
        suggested_ip = ip_from_cidr(suggested_cidr)
        console.print(f"  Suggested: [green]{suggested_cidr}[/green]  — pinging to check for duplicates…")
        if ping_ip(suggested_ip):
            console.print(f"  [yellow]Warning: {suggested_ip} responded to ping — it may already be in use![/yellow]")
            use_suggested = False
        else:
            console.print(f"  [dim]{suggested_ip} did not respond — looks free.[/dim]")
            use_suggested = questionary.confirm(f"Use {suggested_cidr}?", default=True).ask()
            if use_suggested is None:
                sys.exit(0)
    else:
        console.print("[yellow]No available IPs found automatically.[/yellow]")
        use_suggested = False

    def validate_ip(val: str) -> bool | str:
        last_octet = val.strip().split(".")[-1]
        try:
            if int(last_octet) in (0, 1, 255):
                return "Cannot use .0, .1, or .255 — reserved for network/gateway/broadcast"
        except ValueError:
            return "Enter a valid IP address"
        return True

    if use_suggested:
        ip_cidr = suggested_cidr
    else:
        prefix_len = prefix_bits(chosen_prefix["prefix"])
        while True:
            custom_ip = questionary.text(
                f"Enter IP address (will use /{prefix_len}):",
                validate=validate_ip,
            ).ask()
            if custom_ip is None:
                sys.exit(0)
            custom_ip = custom_ip.strip()
            console.print(f"  Pinging {custom_ip}…")
            if ping_ip(custom_ip):
                console.print(f"  [yellow]{custom_ip} responded to ping — try a different address.[/yellow]")
            else:
                console.print(f"  [dim]{custom_ip} did not respond — looks free.[/dim]")
                break
        ip_cidr = f"{custom_ip}/{prefix_len}"

    nb.close()

    ip_only = ip_from_cidr(ip_cidr)
    gateway = gateway_from_cidr(ip_cidr)

    console.print(f"  IP: [green]{ip_cidr}[/green]  Gateway: [green]{gateway}[/green]")

    return {
        "ip_cidr": ip_cidr,
        "ip_only": ip_only,
        "gateway": gateway,
        "prefix_id": chosen_prefix["id"],
    }


def phase1_hardware(cfg: dict, proxmox_node: str) -> dict:
    from lib.proxmox_ssh import ProxmoxSSH

    console.print(Panel("[bold cyan]Hardware & LXC configuration[/bold cyan]", expand=False))
    defaults = cfg.get("defaults", {})
    prox_cfg = cfg["proxmox"]

    # ── Query Proxmox storage ────────────────────────────────────────────────
    all_storage: list[dict] = []
    nfs_storage: list[dict] = []
    try:
        console.print(f"  Querying storage on [bold]{proxmox_node}[/bold]…")
        ssh = ProxmoxSSH(proxmox_node, prox_cfg["ssh_user"], prox_cfg["ssh_key"])
        all_storage = ssh.list_storage()
        ssh.close()
        if not all_storage:
            console.print("  [yellow]Warning: storage query returned no results — falling back to manual entry.[/yellow]")
            console.print(f"  [dim]Test with: ssh {prox_cfg['ssh_user']}@{proxmox_node} pvesh get /storage --output-format json[/dim]")
        else:
            console.print(f"  Found {len(all_storage)} storage pool(s): {', '.join(s['storage'] for s in all_storage)}")
            nfs_storage = [s for s in all_storage if s.get("type") in ("nfs", "cifs")]
            if nfs_storage:
                console.print(f"  NFS/CIFS pools: {', '.join(s['storage'] for s in nfs_storage)}")
            else:
                console.print("  [yellow]No NFS/CIFS storage pools found on this node.[/yellow]")
    except Exception as e:
        console.print(f"  [red]Could not connect to Proxmox to query storage:[/red] {e}")
        console.print(f"  [dim]Make sure your SSH key is authorized on {proxmox_node} and the key path in config.yaml is correct.[/dim]")
        console.print("  Falling back to manual entry.")

    # ── OS disk storage ──────────────────────────────────────────────────────
    disk_storage_pools = [
        s for s in all_storage
        if any(c in s.get("content", "") for c in ("rootdir", "images"))
    ]

    if disk_storage_pools:
        storage_choices = [
            f"{s['storage']}  [{s['type']}]" for s in disk_storage_pools
        ]
        selected_storage = questionary.select(
            "OS disk storage:", choices=storage_choices
        ).ask()
        if selected_storage is None:
            sys.exit(0)
        storage = selected_storage.split()[0]
    else:
        storage = questionary.text(
            "OS disk storage (e.g. local-lvm):",
            default=defaults.get("storage", "local-lvm"),
        ).ask()
        if storage is None:
            sys.exit(0)
        storage = storage.strip()

    # ── Hardware specs ────────────────────────────────────────────────────────
    cpu = questionary.text(
        "CPU cores:",
        default=str(defaults.get("cpu_cores", 2)),
        validate=validate_int(1),
    ).ask()

    ram = questionary.text(
        "RAM (MB):",
        default=str(defaults.get("ram_mb", 2048)),
        validate=validate_int(128),
    ).ask()

    disk = questionary.text(
        "Disk size (GB):",
        default=str(defaults.get("disk_gb", 10)),
        validate=validate_int(1),
    ).ask()

    root_pass = questionary.password("Root password for the LXC:").ask()

    enable_root_ssh = questionary.confirm(
        "Enable root SSH login? (will still require key auth)", default=True
    ).ask()

    # ── NFS / CIFS bind mounts ────────────────────────────────────────────────
    mounts = []
    if nfs_storage:
        nfs_choices = [
            f"{s['storage']}  ({s.get('server', '')}:{s.get('export', s.get('path', ''))})"
            for s in nfs_storage
        ]
        while True:
            add_mount = questionary.confirm(
                f"Add {'another' if mounts else 'an'} NFS/CIFS bind mount?", default=False
            ).ask()
            if not add_mount:
                break
            selected_nfs = questionary.select(
                "Select NFS/CIFS storage to mount:", choices=nfs_choices
            ).ask()
            if selected_nfs is None:
                break
            nfs_name = selected_nfs.split()[0]
            nfs_entry = next(s for s in nfs_storage if s["storage"] == nfs_name)
            mount_dest = questionary.text(
                f"Mount destination inside LXC for '{nfs_name}':",
                default=f"/mnt/{nfs_name}",
            ).ask()
            if mount_dest is None:
                break
            mounts.append({
                "storage": nfs_name,
                "path": nfs_entry.get("path", f"/mnt/pve/{nfs_name}"),
                "dest": mount_dest.strip(),
            })
    else:
        while True:
            add_mount = questionary.confirm(
                f"Add {'another' if mounts else 'an'} NFS/CIFS bind mount?", default=False
            ).ask()
            if not add_mount:
                break
            mount_path = questionary.text("Host path on Proxmox (e.g. /mnt/pve/nfs-data):").ask()
            mount_dest = questionary.text("Mount destination inside LXC (e.g. /mnt/data):").ask()
            mounts.append({"storage": "", "path": mount_path, "dest": mount_dest})

    return {
        "cpu": int(cpu),
        "ram_mb": int(ram),
        "disk_gb": int(disk),
        "storage": storage,
        "root_password": root_pass,
        "permit_root": "yes" if enable_root_ssh else "prohibit-password",
        "mounts": mounts,
    }


def phase1_npmplus(cfg: dict, fqdn: str, ip_only: str) -> dict:
    console.print(Panel("[bold cyan]NPMplus reverse proxy[/bold cyan]", expand=False))

    skip = questionary.confirm("Skip NPMplus proxy setup?", default=False).ask()
    if skip:
        return {"skip": True}

    forward_port = questionary.text(
        "Service port on the LXC (forward to):",
        validate=validate_port,
    ).ask()
    if forward_port is None:
        sys.exit(0)

    scheme = questionary.select(
        "Forward scheme:", choices=["http", "https"], default="http"
    ).ask()

    ssl = questionary.confirm("Force SSL (HTTPS) on the proxy?", default=False).ask()

    return {
        "skip": False,
        "forward_port": int(forward_port),
        "forward_scheme": scheme,
        "ssl_forced": ssl,
    }


# ── Confirmation summary ──────────────────────────────────────────────────────

def show_summary(meta: dict, ip_info: dict, hw: dict, npm_info: dict):
    table = Table(title="Deployment Summary", show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="bold")
    table.add_column("Value")

    table.add_row("Helper script", meta["helper_url"])
    table.add_row("Proxmox node", meta["proxmox_node"])
    table.add_row("FQDN", meta["fqdn"])
    table.add_row("IP", ip_info["ip_cidr"])
    table.add_row("Gateway", ip_info["gateway"])
    table.add_row("CPU cores", str(hw["cpu"]))
    table.add_row("RAM", f"{hw['ram_mb']} MB")
    table.add_row("Disk", f"{hw['disk_gb']} GB")
    table.add_row("Storage", hw["storage"])
    table.add_row("Root SSH login", hw["permit_root"])
    for i, m in enumerate(hw["mounts"], 1):
        table.add_row(f"Mount {i}", f"{m['path']} → {m['dest']}")
    if not npm_info.get("skip"):
        table.add_row("NPMplus proxy", f"→ {ip_info['ip_only']}:{npm_info['forward_port']}")

    console.print(Panel(table, expand=False))


# ── Phase 2: Create LXC ───────────────────────────────────────────────────────

def phase2_create_lxc(cfg: dict, meta: dict, ip_info: dict, hw: dict) -> bool:
    from lib.proxmox_ssh import ProxmoxSSH

    prox_cfg = cfg["proxmox"]
    console.print(Panel("[bold cyan]Phase 2 — Creating LXC on Proxmox[/bold cyan]", expand=False))

    env_vars: dict[str, str] = {
        "HN": meta["hostname"],
        "CT_TYPE": "1",                  # 1 = unprivileged
        "CORE_COUNT": str(hw["cpu"]),
        "RAM_SIZE": str(hw["ram_mb"]),
        "DISK_SIZE": str(hw["disk_gb"]),
        "STORAGE": hw["storage"],
        "NET": f"name=eth0,ip={ip_info['ip_cidr']},gw={ip_info['gateway']},bridge=vmbr0",
        "SSH_ROOT_PW": hw["root_password"],
    }

    console.print(f"  Connecting to Proxmox at [bold]{meta['proxmox_node']}[/bold]…")
    try:
        ssh = ProxmoxSSH(meta["proxmox_node"], prox_cfg["ssh_user"], prox_cfg["ssh_key"])
    except Exception as e:
        console.print(f"[red]SSH connection failed:[/red] {e}")
        return False

    console.print(f"  Running helper script: [dim]{meta['helper_url']}[/dim]\n")
    exit_code = ssh.run_helper_script(meta["helper_url"], env_vars)

    if exit_code != 0:
        ssh.close()
        console.print(f"\n[red]Helper script exited with code {exit_code}.[/red]")
        return False

    console.print(f"\n[green]LXC created successfully.[/green]")

    # Apply bind mounts if any were configured
    if hw.get("mounts"):
        node_short = meta["proxmox_node"].split(".")[0]
        vmid = ssh.find_container_id(node_short, meta["hostname"])
        if vmid:
            console.print(f"  Applying {len(hw['mounts'])} bind mount(s) to VMID {vmid}…")
            errors = ssh.set_container_mounts(vmid, hw["mounts"])
            if errors:
                for err in errors:
                    console.print(f"  [yellow]Mount warning:[/yellow] {err}")
            else:
                console.print(f"  [green]Bind mounts applied.[/green]")
        else:
            console.print(f"  [yellow]Could not find container VMID for '{meta['hostname']}' — bind mounts skipped.[/yellow]")

    ssh.close()
    return True


# ── Phase 3: Register services ────────────────────────────────────────────────

def phase3_adguard(cfg: dict, fqdn: str, ip_only: str):
    from lib.adguard import AdGuardClient

    ag_cfg = cfg["adguard"]
    dns_target = ag_cfg.get("dns_target", ip_only)
    console.print(f"  [bold]AdGuard:[/bold] creating DNS rewrite {fqdn} → {dns_target}…")
    try:
        ag = AdGuardClient(ag_cfg["url"], ag_cfg["username"], ag_cfg["password"])
        if ag.rewrite_exists(fqdn):
            console.print(f"    [yellow]DNS rewrite for {fqdn} already exists, skipping.[/yellow]")
        else:
            ag.add_rewrite(fqdn, dns_target)
            console.print(f"    [green]Done.[/green]")
        ag.close()
    except Exception as e:
        console.print(f"    [red]AdGuard error:[/red] {e}")


def phase3_npmplus(cfg: dict, fqdn: str, ip_only: str, npm_info: dict):
    if npm_info.get("skip"):
        console.print("  [bold]NPMplus:[/bold] skipped.")
        return

    from lib.npmplus import NPMPlusClient

    npm_cfg = cfg["npmplus"]
    console.print(f"  [bold]NPMplus:[/bold] creating proxy host for {fqdn}…")
    try:
        npm = NPMPlusClient(npm_cfg["url"], npm_cfg["email"], npm_cfg["password"])
        npm.create_proxy_host(
            domain_names=[fqdn],
            forward_host=ip_only,
            forward_port=npm_info["forward_port"],
            forward_scheme=npm_info.get("forward_scheme", "http"),
            ssl_forced=npm_info.get("ssl_forced", False),
        )
        npm.close()
        console.print(f"    [green]Done.[/green]")
    except Exception as e:
        console.print(f"    [red]NPMplus error:[/red] {e}")


def phase3_netbox(cfg: dict, fqdn: str, ip_cidr: str, hw: dict, proxmox_node: str):
    from lib.netbox import NetBoxClient

    nb_cfg = cfg["netbox"]
    console.print(f"  [bold]NetBox:[/bold] registering VM {fqdn} with IP {ip_cidr}…")
    try:
        nb = NetBoxClient(nb_cfg["url"], nb_cfg["token"])
        device_id = nb.get_device_id(proxmox_node)
        vm = nb.create_virtual_machine(
            name=fqdn,
            device_id=device_id,
            vcpus=hw["cpu"],
            memory_mb=hw["ram_mb"],
            disk_gb=hw["disk_gb"],
        )
        iface = nb.create_interface(vm["id"])
        ip_obj = nb.create_ip_address(ip_cidr, dns_name=fqdn, interface_id=iface["id"])
        nb.set_primary_ip(vm["id"], ip_obj["id"])
        nb.close()
        console.print(f"    [green]Done.[/green]")
    except Exception as e:
        console.print(f"    [red]NetBox error:[/red] {e}")


# ── Phase 4: SSH hardening ────────────────────────────────────────────────────

def phase4_harden_ssh(cfg: dict, ip_only: str, hw: dict):
    console.print(Panel("[bold cyan]Phase 4 — SSH hardening[/bold cyan]", expand=False))

    ssh_key_paths: list[str] = cfg.get("ssh_keys", [])
    if not ssh_key_paths:
        console.print("[yellow]No SSH keys configured in config.yaml — skipping SSH hardening.[/yellow]")
        return

    # Collect public key content
    key_lines: list[str] = []
    for path in ssh_key_paths:
        expanded = os.path.expanduser(path)
        if not os.path.exists(expanded):
            console.print(f"  [yellow]SSH key not found, skipping:[/yellow] {path}")
            continue
        with open(expanded) as f:
            key_lines.append(f.read().strip())

    if not key_lines:
        console.print("[yellow]No SSH key files found — skipping SSH hardening.[/yellow]")
        return

    console.print(f"  Waiting for LXC at {ip_only} to accept SSH connections…")
    console.print(f"  Deploying {len(key_lines)} SSH key(s) and hardening sshd_config…")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".pub", delete=False) as tf:
        tf.write("\n".join(key_lines) + "\n")
        keys_file = tf.name

    playbook = Path(__file__).parent / "playbooks" / "configure-lxc.yml"
    cmd = [
        "ansible-playbook",
        str(playbook),
        "-i", f"{ip_only},",
        "-e", f"lxc_ip={ip_only}",
        "-e", f"ssh_keys_file={keys_file}",
        "-e", f"permit_root={hw['permit_root']}",
        "-e", f"ansible_password={hw['root_password']}",
        "--ssh-extra-args=-o StrictHostKeyChecking=no",
        "-u", "root",
    ]

    try:
        result = subprocess.run(cmd, check=False)
        if result.returncode == 0:
            console.print("  [green]SSH hardening complete.[/green]")
        else:
            console.print(f"  [red]Ansible playbook exited with code {result.returncode}.[/red]")
            console.print("  You can re-run manually:")
            console.print(f"  [dim]{' '.join(cmd)}[/dim]")
    finally:
        os.unlink(keys_file)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="LXC Deployment Automation")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Collect all inputs and show the deployment plan without making any changes",
    )
    args = parser.parse_args()

    title = "[bold green]LXC Deployment Automation[/bold green]\n[dim]Proxmox · NetBox · AdGuard · NPMplus[/dim]"
    if args.dry_run:
        title += "\n[yellow bold]DRY RUN — no changes will be made[/yellow bold]"
    console.print(Panel(title, expand=False))

    cfg = load_config()

    # ── Phase 1 ──
    meta = phase1_gather(cfg)
    ip_info = phase1_ip(cfg)
    hw = phase1_hardware(cfg, meta["proxmox_node"])
    npm_info = phase1_npmplus(cfg, meta["fqdn"], ip_info["ip_only"])

    console.print()
    show_summary(meta, ip_info, hw, npm_info)

    if args.dry_run:
        console.print(Panel(
            "[yellow]Dry run complete — no LXC was created and no services were modified.[/yellow]\n\n"
            "Actions that would have run:\n"
            f"  1. SSH to [cyan]{meta['proxmox_node']}[/cyan] and run helper script\n"
            f"  2. AdGuard DNS rewrite: [cyan]{meta['fqdn']}[/cyan] → [cyan]{ip_info['ip_only']}[/cyan]\n"
            + (f"  3. NPMplus proxy host: [cyan]{meta['fqdn']}[/cyan] → [cyan]{ip_info['ip_only']}:{npm_info['forward_port']}[/cyan]\n" if not npm_info.get("skip") else "  3. NPMplus: skipped\n")
            + f"  4. NetBox VM [cyan]{meta['fqdn']}[/cyan] on [cyan]{meta['proxmox_node']}[/cyan] with IP [cyan]{ip_info['ip_cidr']}[/cyan]\n"
            f"  5. SSH hardening + key deployment to [cyan]{ip_info['ip_only']}[/cyan]",
            expand=False,
        ))
        sys.exit(0)

    proceed = questionary.confirm("\nProceed with deployment?", default=True).ask()
    if not proceed:
        console.print("[yellow]Aborted.[/yellow]")
        sys.exit(0)

    # ── Phase 2 ──
    console.print()
    ok = phase2_create_lxc(cfg, meta, ip_info, hw)
    if not ok:
        console.print("[red]LXC creation failed. Aborting remaining steps.[/red]")
        sys.exit(1)

    # ── Phase 3 ──
    console.print(Panel("[bold cyan]Phase 3 — Registering services[/bold cyan]", expand=False))
    phase3_adguard(cfg, meta["fqdn"], ip_info["ip_only"])
    phase3_npmplus(cfg, meta["fqdn"], ip_info["ip_only"], npm_info)
    phase3_netbox(cfg, meta["fqdn"], ip_info["ip_cidr"], hw, meta["proxmox_node"])

    # ── Phase 4 ──
    console.print()
    phase4_harden_ssh(cfg, ip_info["ip_only"], hw)

    # ── Done ──
    console.print()
    console.print(Panel(
        f"[bold green]Deployment complete![/bold green]\n\n"
        f"  LXC:    [cyan]{meta['fqdn']}[/cyan] ({ip_info['ip_only']})\n"
        f"  SSH:    [dim]ssh root@{ip_info['ip_only']}[/dim]\n"
        f"  DNS:    [dim]{meta['fqdn']}[/dim]",
        expand=False,
    ))


if __name__ == "__main__":
    main()
