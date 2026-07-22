#!/usr/bin/env python3
"""
Decommission: remove a VM/LXC's entries from NetBox, AdGuard, and NPMplus
after you've manually deleted it in Proxmox.
"""

import argparse
import re
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


def validate_fqdn(val: str) -> bool | str:
    val = val.strip()
    if re.match(r"^[a-z0-9]([a-z0-9\-\.]*[a-z0-9])?$", val, re.IGNORECASE) and "." in val:
        return True
    return "Enter a valid FQDN (e.g. myapp.lab.home)"


def gather_findings(cfg: dict, fqdn: str) -> dict:
    from lib.adguard import AdGuardClient
    from lib.netbox import NetBoxClient
    from lib.npmplus import NPMPlusClient

    findings: dict = {}

    console.print("  Looking up NetBox…")
    nb_cfg = cfg["netbox"]
    nb = NetBoxClient(nb_cfg["url"], nb_cfg["token"])
    try:
        findings["vm"] = nb.find_vm_by_name(fqdn)
        findings["ip"] = nb.find_ip_by_dns_name(fqdn)
    except Exception as e:
        console.print(f"    [red]NetBox error:[/red] {e}")
        findings["vm"] = None
        findings["ip"] = None
    finally:
        nb.close()

    console.print("  Looking up AdGuard…")
    ag_cfg = cfg["adguard"]
    ag = AdGuardClient(ag_cfg["url"], ag_cfg["username"], ag_cfg["password"])
    try:
        findings["rewrite"] = ag.find_rewrite(fqdn)
    except Exception as e:
        console.print(f"    [red]AdGuard error:[/red] {e}")
        findings["rewrite"] = None
    finally:
        ag.close()

    console.print("  Looking up NPMplus…")
    npm_cfg = cfg["npmplus"]
    try:
        npm = NPMPlusClient(npm_cfg["url"], npm_cfg["email"], npm_cfg["password"])
        findings["proxy_host_id"] = npm.find_proxy_host_id(fqdn)
        npm.close()
    except Exception as e:
        console.print(f"    [red]NPMplus error:[/red] {e}")
        findings["proxy_host_id"] = None

    return findings


def show_summary(fqdn: str, findings: dict) -> bool:
    table = Table(title=f"Found for {fqdn}", show_header=False, box=None, padding=(0, 2))
    table.add_column("Service", style="bold")
    table.add_column("Entry")

    any_found = False

    vm = findings.get("vm")
    if vm:
        table.add_row("NetBox VM", f"{vm['name']} (id {vm['id']})")
        any_found = True
    ip = findings.get("ip")
    if ip:
        table.add_row("NetBox IP", f"{ip['address']} (id {ip['id']})")
        any_found = True
    rewrite = findings.get("rewrite")
    if rewrite:
        table.add_row("AdGuard rewrite", f"{rewrite['domain']} → {rewrite['answer']}")
        any_found = True
    proxy_id = findings.get("proxy_host_id")
    if proxy_id:
        table.add_row("NPMplus proxy host", f"id {proxy_id}")
        any_found = True

    if not any_found:
        console.print(Panel(f"[yellow]Nothing found for {fqdn} in NetBox, AdGuard, or NPMplus.[/yellow]", expand=False))
        return False

    console.print(Panel(table, expand=False))
    return True


def decommission(cfg: dict, fqdn: str, findings: dict):
    from lib.adguard import AdGuardClient
    from lib.netbox import NetBoxClient
    from lib.npmplus import NPMPlusClient

    rewrite = findings.get("rewrite")
    if rewrite:
        console.print(f"  [bold]AdGuard:[/bold] removing {rewrite['domain']} → {rewrite['answer']}…")
        ag_cfg = cfg["adguard"]
        try:
            ag = AdGuardClient(ag_cfg["url"], ag_cfg["username"], ag_cfg["password"])
            ag.remove_rewrite(rewrite["domain"], rewrite["answer"])
            ag.close()
            console.print("    [green]Done.[/green]")
        except Exception as e:
            console.print(f"    [red]AdGuard error:[/red] {e}")

    proxy_id = findings.get("proxy_host_id")
    if proxy_id:
        console.print(f"  [bold]NPMplus:[/bold] removing proxy host id {proxy_id}…")
        npm_cfg = cfg["npmplus"]
        try:
            npm = NPMPlusClient(npm_cfg["url"], npm_cfg["email"], npm_cfg["password"])
            npm.delete_proxy_host(proxy_id)
            npm.close()
            console.print("    [green]Done.[/green]")
        except Exception as e:
            console.print(f"    [red]NPMplus error:[/red] {e}")

    ip = findings.get("ip")
    vm = findings.get("vm")
    if ip or vm:
        console.print("  [bold]NetBox:[/bold] cleaning up…")
        nb_cfg = cfg["netbox"]
        try:
            nb = NetBoxClient(nb_cfg["url"], nb_cfg["token"])
            if ip:
                nb.delete_ip_address(ip["id"])
                console.print(f"    [green]Deleted IP {ip['address']}.[/green]")
            if vm:
                nb.delete_virtual_machine(vm["id"])
                console.print(f"    [green]Deleted VM {vm['name']}.[/green]")
            nb.close()
        except Exception as e:
            console.print(f"    [red]NetBox error:[/red] {e}")


def main():
    parser = argparse.ArgumentParser(description="Decommission: remove NetBox/AdGuard/NPMplus entries for a VM/LXC")
    parser.add_argument("fqdn", nargs="?", help="FQDN of the VM/LXC to decommission")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be removed without making changes")
    args = parser.parse_args()

    title = "[bold red]VM/LXC Decommission[/bold red]\n[dim]Removes NetBox · AdGuard · NPMplus entries — delete the VM/LXC in Proxmox yourself[/dim]"
    if args.dry_run:
        title += "\n[yellow bold]DRY RUN — no changes will be made[/yellow bold]"
    console.print(Panel(title, expand=False))

    cfg = load_config()

    if args.fqdn:
        fqdn = args.fqdn.strip().lower()
        valid = validate_fqdn(fqdn)
        if valid is not True:
            console.print(f"[red]{valid}[/red]")
            sys.exit(1)
    else:
        fqdn = questionary.text("FQDN of the VM/LXC to decommission:", validate=validate_fqdn).ask()
        if fqdn is None:
            sys.exit(0)
        fqdn = fqdn.strip().lower()

    console.print()
    findings = gather_findings(cfg, fqdn)
    console.print()

    if not show_summary(fqdn, findings):
        sys.exit(0)

    if args.dry_run:
        console.print(Panel("[yellow]Dry run — nothing was removed.[/yellow]", expand=False))
        sys.exit(0)

    proceed = questionary.confirm(
        f"\nPermanently remove these entries for {fqdn}? This does not touch Proxmox.",
        default=False,
    ).ask()
    if not proceed:
        console.print("[yellow]Aborted.[/yellow]")
        sys.exit(0)

    console.print()
    console.print(Panel("[bold cyan]Removing entries[/bold cyan]", expand=False))
    decommission(cfg, fqdn, findings)

    console.print()
    console.print(Panel(
        f"[bold green]Done.[/bold green] {fqdn} has been cleaned up in NetBox, AdGuard, and NPMplus.\n"
        f"[dim]Don't forget to delete the VM/LXC in Proxmox if you haven't already.[/dim]",
        expand=False,
    ))


if __name__ == "__main__":
    main()
