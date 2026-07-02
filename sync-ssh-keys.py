#!/usr/bin/env python3
"""
Sync SSH keys: ensure every key in config.yaml's ssh_keys is present in
/root/.ssh/authorized_keys on every host in config.yaml's hosts list.

Existing keys on each host are left alone — only missing ones are added.
Connects using your current SSH identity (agent/default keys), so hosts
must already trust one of your existing keys.

Usage:
    python3 sync-ssh-keys.py                # sync all hosts in config.yaml
    python3 sync-ssh-keys.py 192.168.1.50    # sync a single host
    python3 sync-ssh-keys.py --dry-run       # show what would change
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

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


def main():
    parser = argparse.ArgumentParser(description="Ensure configured SSH keys are installed on all hosts")
    parser.add_argument("host", nargs="?", help="Sync only this host/IP instead of everything in config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without applying it")
    args = parser.parse_args()

    console.print(Panel(
        "[bold green]SSH Key Sync[/bold green]\n[dim]Adds any missing configured keys to each host's authorized_keys[/dim]",
        expand=False,
    ))

    cfg = load_config()

    hosts: list[str] = [args.host] if args.host else cfg.get("hosts", [])
    if not hosts:
        console.print("[red]No hosts to sync.[/red] Pass a host as an argument or add a `hosts:` list to config.yaml.")
        sys.exit(1)

    ssh_key_paths: list[str] = cfg.get("ssh_keys", [])
    if not ssh_key_paths:
        console.print("[yellow]No SSH keys configured in config.yaml — nothing to sync.[/yellow]")
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

    console.print(f"\n  Checking {len(hosts)} host(s) for {len(key_lines)} key(s)…")
    for h in hosts:
        console.print(f"    [cyan]{h}[/cyan]")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".pub", delete=False) as tf:
        tf.write("\n".join(key_lines) + "\n")
        keys_file = tf.name

    playbook = Path(__file__).parent / "playbooks" / "sync-ssh-keys.yml"
    inventory = ",".join(hosts) + ","
    cmd = [
        "ansible-playbook",
        str(playbook),
        "-i", inventory,
        "-e", f"ssh_keys_file={keys_file}",
        "--ssh-extra-args=-o StrictHostKeyChecking=accept-new",
        "-u", "root",
    ]
    if args.dry_run:
        cmd += ["--check", "--diff"]

    try:
        result = subprocess.run(cmd, check=False)
        if result.returncode == 0:
            console.print(Panel("[bold green]SSH key sync complete![/bold green]", expand=False))
        else:
            console.print(f"[red]Ansible playbook failed (exit code {result.returncode}).[/red]")
            console.print("Re-run manually:")
            console.print(f"  [dim]{' '.join(cmd)}[/dim]")
    finally:
        os.unlink(keys_file)


if __name__ == "__main__":
    main()
