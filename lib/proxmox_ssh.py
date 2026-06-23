import json
import os
import sys
import select
import paramiko


class ProxmoxSSH:
    def __init__(self, host: str, user: str, key_path: str):
        self._host = host
        self._user = user
        self._key_path = key_path
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            hostname=host,
            username=user,
            key_filename=os.path.expanduser(key_path),
        )

    def close(self):
        self._client.close()

    def run_helper_script(self, url: str, env_vars: dict[str, str]) -> int:
        """
        Download and run a Proxmox community helper script on the Proxmox host.
        Streams stdout/stderr live so the user can answer interactive prompts.
        Returns the script's exit code.

        env_vars pre-fill known variables so the script can run with fewer
        interactive prompts; any variable the script still needs will be asked
        interactively in the user's terminal.
        """
        env_exports = " ".join(f'{k}="{v}"' for k, v in env_vars.items())
        command = f'export {env_exports}; bash -c "$(curl -fsSL {url})"'

        transport = self._client.get_transport()
        channel = transport.open_session()
        channel.get_pty(term=os.environ.get("TERM", "xterm-256color"), width=220, height=50)
        channel.exec_command(command)

        # Stream output and forward stdin so interactive prompts work
        while True:
            r, _, _ = select.select([channel, sys.stdin], [], [], 0.1)

            if channel in r:
                if channel.recv_ready():
                    data = channel.recv(1024)
                    if data:
                        sys.stdout.buffer.write(data)
                        sys.stdout.buffer.flush()
                if channel.recv_stderr_ready():
                    data = channel.recv_stderr(1024)
                    if data:
                        sys.stderr.buffer.write(data)
                        sys.stderr.buffer.flush()

            if sys.stdin in r:
                data = sys.stdin.buffer.read1(1024)
                if data:
                    channel.sendall(data)

            if channel.exit_status_ready():
                # Drain any remaining output
                while channel.recv_ready():
                    sys.stdout.buffer.write(channel.recv(4096))
                sys.stdout.buffer.flush()
                break

        return channel.recv_exit_status()

    def run(self, command: str) -> tuple[int, str, str]:
        """Run a non-interactive command and return (exit_code, stdout, stderr)."""
        _, stdout, stderr = self._client.exec_command(command)
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, stdout.read().decode(), stderr.read().decode()

    def list_storage(self) -> list[dict]:
        """Parse /etc/pve/storage.cfg and return storage pool list.

        Uses direct file read instead of pvesh, which requires a full cluster
        environment that isn't available in non-interactive SSH sessions.
        """
        code, out, _ = self.run("cat /etc/pve/storage.cfg 2>/dev/null")
        if code != 0 or not out.strip():
            return []

        storages: list[dict] = []
        current: dict = {}
        for line in out.splitlines():
            stripped = line.rstrip()
            if not stripped:
                if current:
                    storages.append(current)
                    current = {}
                continue
            if stripped[0] not in (" ", "\t"):
                # Header line: "type: storagename"
                if current:
                    storages.append(current)
                parts = stripped.split(":", 1)
                current = {"type": parts[0].strip(), "storage": parts[1].strip()}
            else:
                # Property line: "\tkey value"
                parts = stripped.strip().split(None, 1)
                if len(parts) == 2:
                    current[parts[0]] = parts[1]
        if current:
            storages.append(current)

        return storages

    def find_container_id(self, node: str, hostname: str) -> int | None:
        """Find a container's VMID by searching /etc/pve/lxc/*.conf for the hostname."""
        code, out, _ = self.run(
            f"grep -rl '^hostname: {hostname}$' /etc/pve/lxc/ 2>/dev/null"
        )
        if code != 0 or not out.strip():
            return None
        try:
            conf_path = out.strip().splitlines()[0]
            return int(conf_path.split("/")[-1].replace(".conf", ""))
        except (ValueError, IndexError):
            return None

    def set_container_mounts(self, vmid: int, mounts: list[dict]) -> list[str]:
        """
        Add bind mounts to a container via pct set.
        Each mount dict: {"path": "/mnt/pve/storage-name", "dest": "/mnt/data"}
        Returns list of any error messages.
        """
        errors = []
        for i, mount in enumerate(mounts):
            cmd = f"pct set {vmid} -mp{i} {mount['path']},mp={mount['dest']}"
            code, _, err = self.run(cmd)
            if code != 0:
                errors.append(f"mp{i}: {err.strip()}")
        return errors
