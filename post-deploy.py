#!/usr/bin/env python3
"""
Post-deployment: deploy SSH keys and harden sshd on a newly created VM/LXC.
Usage: python3 post-deploy.py [IP]
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import questionary
import yaml
from rich.console import Console
from rich.panel import Panel

console = Console()

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        console.print("[red]config.yaml not found.[/red] Copy config.example.yaml → config.yaml and fill in your values.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def validate_ip(val: str) -> bool | str:
    parts = val.strip().split(".")
    if len(parts) != 4:
        return "Enter a valid IP address (e.g. 192.168.20.18)"
    try:
        if all(0 <= int(p) <= 255 for p in parts):
            return True
    except ValueError:
        pass
    return "Enter a valid IP address"


def main():
    parser = argparse.ArgumentParser(description="Post-deployment: SSH key deployment and hardening")
    parser.add_argument("ip", nargs="?", help="IP address of the new VM/LXC")
    args = parser.parse_args()

    console.print(Panel(
        "[bold green]VM/LXC Post-Deployment[/bold green]\n[dim]SSH key deployment · sshd hardening[/dim]",
        expand=False,
    ))

    cfg = load_config()

    # Get IP
    if args.ip:
        ip = args.ip.strip()
        valid = validate_ip(ip)
        if valid is not True:
            console.print(f"[red]{valid}[/red]")
            sys.exit(1)
        console.print(f"  Target VM/LXC: [cyan]{ip}[/cyan]")
    else:
        ip = questionary.text("IP address of the new VM/LXC:", validate=validate_ip).ask()
        if ip is None:
            sys.exit(0)
        ip = ip.strip()

    # Root password (needed for initial Ansible connection before key auth is set up)
    root_pass = questionary.password("Root password of the VM/LXC (for initial connection):").ask()
    if root_pass is None:
        sys.exit(0)

    permit_root = questionary.select(
        "PermitRootLogin setting:",
        choices=[
            "prohibit-password  (key auth only — recommended)",
            "yes  (password + key)",
            "no  (disable root SSH entirely)",
        ],
    ).ask()
    if permit_root is None:
        sys.exit(0)
    permit_root_value = permit_root.split()[0]

    # Collect SSH keys from config
    ssh_key_paths: list[str] = cfg.get("ssh_keys", [])
    if not ssh_key_paths:
        console.print("[yellow]No SSH keys configured in config.yaml — nothing to deploy.[/yellow]")
        sys.exit(1)

    key_lines: list[str] = []
    for path in ssh_key_paths:
        expanded = os.path.expanduser(path)
        if not os.path.exists(expanded):
            console.print(f"  [yellow]Key not found, skipping:[/yellow] {path}")
            continue
        with open(expanded) as f:
            key_lines.append(f.read().strip())

    if not key_lines:
        console.print("[red]No SSH key files found — check ssh_keys in config.yaml.[/red]")
        sys.exit(1)

    console.print(f"\n  Deploying {len(key_lines)} key(s) to [cyan]{ip}[/cyan] and hardening sshd…")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".pub", delete=False) as tf:
        tf.write("\n".join(key_lines) + "\n")
        keys_file = tf.name

    playbook = Path(__file__).parent / "playbooks" / "configure-lxc.yml"
    cmd = [
        "ansible-playbook",
        str(playbook),
        "-i", f"{ip},",
        "-e", f"lxc_ip={ip}",
        "-e", f"ssh_keys_file={keys_file}",
        "-e", f"permit_root={permit_root_value}",
        "-e", f"ansible_password={root_pass}",
        "--ssh-extra-args=-o StrictHostKeyChecking=no",
        "-u", "root",
    ]

    try:
        result = subprocess.run(cmd, check=False)
        if result.returncode == 0:
            console.print(Panel(
                f"[bold green]SSH hardening complete![/bold green]\n\n"
                f"  [dim]ssh root@{ip}[/dim]  (key auth only)",
                expand=False,
            ))
        else:
            console.print(f"[red]Ansible playbook failed (exit code {result.returncode}).[/red]")
            console.print("Re-run manually:")
            console.print(f"  [dim]{' '.join(cmd)}[/dim]")
    finally:
        os.unlink(keys_file)


if __name__ == "__main__":
    main()
