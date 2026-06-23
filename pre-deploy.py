#!/usr/bin/env python3
"""
Pre-deployment: reserve an IP, register DNS/proxy/NetBox, then print the
settings to use when manually creating the LXC in Proxmox.
"""

import argparse
import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

import questionary
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        console.print("[red]config.yaml not found.[/red] Copy config.example.yaml → config.yaml and fill in your values.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def ip_from_cidr(cidr: str) -> str:
    return cidr.split("/")[0]


def gateway_from_cidr(cidr: str) -> str:
    net = ipaddress.ip_interface(cidr).network
    return str(next(net.hosts()))


def prefix_bits(cidr: str) -> str:
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
        if 1 <= int(val) <= 65535:
            return True
    except ValueError:
        pass
    return "Enter a port number between 1 and 65535"


def ping_ip(ip: str) -> bool:
    result = subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True)
    return result.returncode == 0


# ── Phase 1: Gather info ──────────────────────────────────────────────────────

def gather_info(cfg: dict) -> dict:
    console.print(Panel("[bold cyan]Step 1 — Deployment info[/bold cyan]", expand=False))

    default_suffix = cfg.get("defaults", {}).get("domain_suffix", "")

    proxmox_node = questionary.text(
        "Proxmox node FQDN (parent host for this LXC):",
        default=cfg.get("proxmox", {}).get("host", ""),
        validate=validate_fqdn,
    ).ask()
    if proxmox_node is None:
        sys.exit(0)

    fqdn_hint = f"  (e.g. myapp.{default_suffix})" if default_suffix else ""
    fqdn = questionary.text(
        f"FQDN for the new LXC{fqdn_hint}:",
        validate=validate_fqdn,
    ).ask()
    if fqdn is None:
        sys.exit(0)
    fqdn = fqdn.strip().lower()
    hostname = fqdn.split(".")[0]

    return {"proxmox_node": proxmox_node.strip().lower(), "fqdn": fqdn, "hostname": hostname}


def gather_ip(cfg: dict) -> dict:
    from lib.netbox import NetBoxClient

    nb_cfg = cfg["netbox"]
    nb = NetBoxClient(nb_cfg["url"], nb_cfg["token"])

    console.print(Panel("[bold cyan]Step 2 — IP selection[/bold cyan]", expand=False))
    console.print("  Fetching subnets from NetBox…")
    try:
        prefixes = nb.list_prefixes()
    except Exception as e:
        console.print(f"[red]NetBox error:[/red] {e}")
        sys.exit(1)

    if not prefixes:
        console.print("[red]No active prefixes found in NetBox.[/red]")
        sys.exit(1)

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
    chosen_prefix = prefixes[int(selected.split(".")[0]) - 1]

    console.print(f"\n  Finding next available IP in {chosen_prefix['prefix']}…")
    try:
        suggested_cidr = nb.next_available_ip(chosen_prefix["id"])
    except Exception as e:
        console.print(f"[red]NetBox error:[/red] {e}")
        sys.exit(1)

    def validate_ip(val: str) -> bool | str:
        try:
            if int(val.strip().split(".")[-1]) in (0, 1, 255):
                return "Cannot use .0, .1, or .255 — reserved"
        except ValueError:
            return "Enter a valid IP address"
        return True

    use_suggested = False
    if suggested_cidr:
        suggested_ip = ip_from_cidr(suggested_cidr)
        console.print(f"  Suggested: [green]{suggested_cidr}[/green]  — pinging…")
        if ping_ip(suggested_ip):
            console.print(f"  [yellow]{suggested_ip} responded to ping — may already be in use.[/yellow]")
        else:
            console.print(f"  [dim]{suggested_ip} did not respond — looks free.[/dim]")
            use_suggested = questionary.confirm(f"Use {suggested_cidr}?", default=True).ask()
            if use_suggested is None:
                sys.exit(0)
    else:
        console.print("  [yellow]No available IPs found automatically.[/yellow]")

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

    return {"ip_cidr": ip_cidr, "ip_only": ip_only, "gateway": gateway, "prefix_id": chosen_prefix["id"]}


def gather_npmplus(cfg: dict, fqdn: str) -> dict:
    console.print(Panel("[bold cyan]Step 3 — NPMplus reverse proxy[/bold cyan]", expand=False))

    skip = questionary.confirm("Skip NPMplus proxy setup?", default=False).ask()
    if skip:
        return {"skip": True}

    forward_port = questionary.text("Service port on the LXC:", validate=validate_port).ask()
    if forward_port is None:
        sys.exit(0)

    scheme = questionary.select("Forward scheme:", choices=["http", "https"], default="http").ask()
    ssl = questionary.confirm("Force SSL (HTTPS) on the proxy?", default=False).ask()

    return {
        "skip": False,
        "forward_port": int(forward_port),
        "forward_scheme": scheme,
        "ssl_forced": ssl,
    }


# ── Summary ───────────────────────────────────────────────────────────────────

def show_summary(meta: dict, ip_info: dict, npm_info: dict):
    table = Table(title="Pre-Deployment Summary", show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="bold")
    table.add_column("Value")

    table.add_row("Proxmox node", meta["proxmox_node"])
    table.add_row("FQDN", meta["fqdn"])
    table.add_row("IP", ip_info["ip_cidr"])
    table.add_row("Gateway", ip_info["gateway"])
    if not npm_info.get("skip"):
        table.add_row("NPMplus proxy", f"→ {ip_info['ip_only']}:{npm_info['forward_port']}")

    console.print(Panel(table, expand=False))


# ── Registration ──────────────────────────────────────────────────────────────

def register_adguard(cfg: dict, fqdn: str, ip_only: str):
    from lib.adguard import AdGuardClient
    ag_cfg = cfg["adguard"]
    dns_target = ag_cfg.get("dns_target", ip_only)
    console.print(f"  [bold]AdGuard:[/bold] {fqdn} → {dns_target}…")
    try:
        ag = AdGuardClient(ag_cfg["url"], ag_cfg["username"], ag_cfg["password"])
        if ag.rewrite_exists(fqdn):
            console.print(f"    [yellow]Already exists, skipping.[/yellow]")
        else:
            ag.add_rewrite(fqdn, dns_target)
            console.print(f"    [green]Done.[/green]")
        ag.close()
    except Exception as e:
        console.print(f"    [red]AdGuard error:[/red] {e}")


def register_npmplus(cfg: dict, fqdn: str, ip_only: str, npm_info: dict):
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


def register_netbox(cfg: dict, meta: dict, ip_info: dict):
    from lib.netbox import NetBoxClient
    nb_cfg = cfg["netbox"]
    console.print(f"  [bold]NetBox:[/bold] creating VM {meta['fqdn']} on {meta['proxmox_node']}…")
    try:
        nb = NetBoxClient(nb_cfg["url"], nb_cfg["token"])
        device_id = nb.get_device_id(meta["proxmox_node"])
        vm = nb.create_virtual_machine(
            name=meta["fqdn"],
            device_id=device_id,
            vcpus=2,
            memory_mb=2048,
            disk_gb=10,
        )
        iface = nb.create_interface(vm["id"])
        ip_obj = nb.create_ip_address(ip_info["ip_cidr"], dns_name=meta["fqdn"], interface_id=iface["id"])
        nb.set_primary_ip(vm["id"], ip_obj["id"])
        nb.close()
        console.print(f"    [green]Done.[/green]")
    except Exception as e:
        console.print(f"    [red]NetBox error:[/red] {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pre-deployment: reserve IP and register services")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without making changes")
    args = parser.parse_args()

    title = "[bold green]LXC Pre-Deployment[/bold green]\n[dim]NetBox · AdGuard · NPMplus[/dim]"
    if args.dry_run:
        title += "\n[yellow bold]DRY RUN — no changes will be made[/yellow bold]"
    console.print(Panel(title, expand=False))

    cfg = load_config()

    meta = gather_info(cfg)
    ip_info = gather_ip(cfg)
    npm_info = gather_npmplus(cfg, meta["fqdn"])

    console.print()
    show_summary(meta, ip_info, npm_info)

    if args.dry_run:
        console.print(Panel(
            "[yellow]Dry run — nothing was registered.[/yellow]\n\n"
            "Would have created:\n"
            f"  AdGuard DNS rewrite: [cyan]{meta['fqdn']}[/cyan] → [cyan]{cfg['adguard'].get('dns_target', ip_info['ip_only'])}[/cyan]\n"
            + (f"  NPMplus proxy host: [cyan]{meta['fqdn']}[/cyan] → [cyan]{ip_info['ip_only']}:{npm_info['forward_port']}[/cyan]\n" if not npm_info.get("skip") else "  NPMplus: skipped\n")
            + f"  NetBox VM [cyan]{meta['fqdn']}[/cyan] with IP [cyan]{ip_info['ip_cidr']}[/cyan]",
            expand=False,
        ))
        sys.exit(0)

    proceed = questionary.confirm("\nRegister in AdGuard, NPMplus, and NetBox now?", default=True).ask()
    if not proceed:
        console.print("[yellow]Aborted.[/yellow]")
        sys.exit(0)

    console.print(Panel("[bold cyan]Registering services[/bold cyan]", expand=False))
    register_adguard(cfg, meta["fqdn"], ip_info["ip_only"])
    register_npmplus(cfg, meta["fqdn"], ip_info["ip_only"], npm_info)
    register_netbox(cfg, meta, ip_info)

    console.print()
    console.print(Panel(
        f"[bold green]Done! Now create your LXC manually with these settings:[/bold green]\n\n"
        f"  Hostname:  [cyan]{meta['hostname']}[/cyan]\n"
        f"  IP:        [cyan]{ip_info['ip_cidr']}[/cyan]\n"
        f"  Gateway:   [cyan]{ip_info['gateway']}[/cyan]\n"
        f"  DNS:       [cyan]{cfg['adguard'].get('dns_target', ip_info['ip_only'])}[/cyan]\n\n"
        f"When the LXC is up, run:  [dim]python3 post-deploy.py {ip_info['ip_only']}[/dim]",
        expand=False,
    ))


if __name__ == "__main__":
    main()
