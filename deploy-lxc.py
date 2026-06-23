#!/usr/bin/env python3
"""
LXC Deployment Orchestrator
Automates: IP selection (NetBox) → LXC creation (Proxmox helper script) →
           DNS (AdGuard) → reverse proxy (NPMplus) → IPAM registration (NetBox)
           → SSH hardening (Ansible)
"""

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

    if suggested_cidr:
        console.print(f"  Suggested: [green]{suggested_cidr}[/green]")
        use_suggested = questionary.confirm(
            f"Use {suggested_cidr}?", default=True
        ).ask()
        if use_suggested is None:
            sys.exit(0)
    else:
        console.print("[yellow]No available IPs found automatically.[/yellow]")
        use_suggested = False

    if use_suggested:
        ip_cidr = suggested_cidr
    else:
        prefix_len = prefix_bits(chosen_prefix["prefix"])
        custom_ip = questionary.text(
            f"Enter IP address (will use /{prefix_len}):",
        ).ask()
        if custom_ip is None:
            sys.exit(0)
        ip_cidr = f"{custom_ip.strip()}/{prefix_len}"

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


def phase1_hardware(cfg: dict) -> dict:
    console.print(Panel("[bold cyan]Hardware & LXC configuration[/bold cyan]", expand=False))

    defaults = cfg.get("defaults", {})

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

    # NFS/CIFS mounts
    mounts = []
    while True:
        add_mount = questionary.confirm(
            f"Add {'another' if mounts else 'an'} NFS/CIFS mount?", default=False
        ).ask()
        if not add_mount:
            break
        mount_source = questionary.text("Mount source (e.g. 192.168.10.5:/volume1/data):").ask()
        mount_dest = questionary.text("Mount destination inside LXC (e.g. /mnt/data):").ask()
        mounts.append({"source": mount_source, "dest": mount_dest})

    return {
        "cpu": int(cpu),
        "ram_mb": int(ram),
        "disk_gb": int(disk),
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
    table.add_row("FQDN", meta["fqdn"])
    table.add_row("IP", ip_info["ip_cidr"])
    table.add_row("Gateway", ip_info["gateway"])
    table.add_row("CPU cores", str(hw["cpu"]))
    table.add_row("RAM", f"{hw['ram_mb']} MB")
    table.add_row("Disk", f"{hw['disk_gb']} GB")
    table.add_row("Root SSH login", hw["permit_root"])
    for i, m in enumerate(hw["mounts"], 1):
        table.add_row(f"Mount {i}", f"{m['source']} → {m['dest']}")
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
        "NET": f"name=eth0,ip={ip_info['ip_cidr']},gw={ip_info['gateway']},bridge=vmbr0",
        "SSH_ROOT_PW": hw["root_password"],
    }

    console.print(f"  Connecting to Proxmox at [bold]{prox_cfg['host']}[/bold]…")
    try:
        ssh = ProxmoxSSH(prox_cfg["host"], prox_cfg["ssh_user"], prox_cfg["ssh_key"])
    except Exception as e:
        console.print(f"[red]SSH connection failed:[/red] {e}")
        return False

    console.print(f"  Running helper script: [dim]{meta['helper_url']}[/dim]\n")
    exit_code = ssh.run_helper_script(meta["helper_url"], env_vars)
    ssh.close()

    if exit_code != 0:
        console.print(f"\n[red]Helper script exited with code {exit_code}.[/red]")
        return False

    console.print(f"\n[green]LXC created successfully.[/green]")
    return True


# ── Phase 3: Register services ────────────────────────────────────────────────

def phase3_adguard(cfg: dict, fqdn: str, ip_only: str):
    from lib.adguard import AdGuardClient

    console.print(f"  [bold]AdGuard:[/bold] creating DNS rewrite {fqdn} → {ip_only}…")
    ag_cfg = cfg["adguard"]
    try:
        ag = AdGuardClient(ag_cfg["url"], ag_cfg["username"], ag_cfg["password"])
        if ag.rewrite_exists(fqdn):
            console.print(f"    [yellow]DNS rewrite for {fqdn} already exists, skipping.[/yellow]")
        else:
            ag.add_rewrite(fqdn, ip_only)
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


def phase3_netbox(cfg: dict, fqdn: str, ip_cidr: str, hw: dict):
    from lib.netbox import NetBoxClient

    nb_cfg = cfg["netbox"]
    console.print(f"  [bold]NetBox:[/bold] registering VM {fqdn} with IP {ip_cidr}…")
    try:
        nb = NetBoxClient(nb_cfg["url"], nb_cfg["token"])
        cluster_id = nb.get_cluster_id(nb_cfg.get("cluster_name", "proxmox"))
        vm = nb.create_virtual_machine(
            name=fqdn,
            cluster_id=cluster_id,
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
    console.print(Panel(
        "[bold green]LXC Deployment Automation[/bold green]\n"
        "[dim]Proxmox · NetBox · AdGuard · NPMplus[/dim]",
        expand=False,
    ))

    cfg = load_config()

    # ── Phase 1 ──
    meta = phase1_gather(cfg)
    ip_info = phase1_ip(cfg)
    hw = phase1_hardware(cfg)
    npm_info = phase1_npmplus(cfg, meta["fqdn"], ip_info["ip_only"])

    console.print()
    show_summary(meta, ip_info, hw, npm_info)

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
    phase3_netbox(cfg, meta["fqdn"], ip_info["ip_cidr"], hw)

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
