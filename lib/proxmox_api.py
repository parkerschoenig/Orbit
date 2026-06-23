import httpx


class ProxmoxAPI:
    """Thin Proxmox REST API client for storage and container queries."""

    def __init__(self, host: str, token_id: str, token_secret: str, port: int = 8006):
        self._base = f"https://{host}:{port}/api2/json"
        self._client = httpx.Client(
            headers={"Authorization": f"PVEAPIToken={token_id}={token_secret}"},
            verify=False,
            timeout=10,
        )

    def close(self):
        self._client.close()

    def list_storage(self) -> list[dict]:
        """Return all configured storage pools."""
        resp = self._client.get(f"{self._base}/storage")
        resp.raise_for_status()
        return resp.json()["data"]

    def find_container_id(self, node: str, hostname: str) -> int | None:
        """Find a container's VMID by hostname on a given node."""
        resp = self._client.get(f"{self._base}/nodes/{node}/lxc")
        resp.raise_for_status()
        for ct in resp.json()["data"]:
            if ct.get("name") == hostname:
                return int(ct["vmid"])
        return None
