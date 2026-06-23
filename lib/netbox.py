import httpx
from typing import Optional


class NetBoxClient:
    def __init__(self, url: str, token: str):
        self._base = url.rstrip("/") + "/api"
        self._client = httpx.Client(
            headers={"Authorization": f"Token {token}", "Accept": "application/json"},
            verify=False,
            timeout=12,
        )

    def close(self):
        self._client.close()

    # ── Prefixes / IP selection ───────────────────────────────────────────────

    def list_prefixes(self) -> list[dict]:
        """Return all active prefixes, sorted by prefix."""
        resp = self._client.get(f"{self._base}/ipam/prefixes/", params={"limit": 500, "status": "active"})
        resp.raise_for_status()
        return resp.json()["results"]

    def next_available_ip(self, prefix_id: int) -> Optional[str]:
        """Return the next free IP (CIDR notation) in a prefix, skipping reserved addresses (.0, .1, .255)."""
        resp = self._client.get(
            f"{self._base}/ipam/prefixes/{prefix_id}/available-ips/",
            params={"limit": 20},
        )
        resp.raise_for_status()
        results = resp.json()
        for entry in results:
            last_octet = int(entry["address"].split("/")[0].split(".")[-1])
            if last_octet not in (0, 1, 255):
                return entry["address"]
        return None

    # ── VM / Interface / IP creation ─────────────────────────────────────────

    def get_device_id(self, device_name: str) -> int:
        resp = self._client.get(f"{self._base}/dcim/devices/", params={"name": device_name})
        resp.raise_for_status()
        results = resp.json()["results"]
        if not results:
            raise ValueError(f"NetBox device '{device_name}' not found")
        return results[0]["id"]

    def create_virtual_machine(self, name: str, device_id: int, vcpus: int, memory_mb: int, disk_gb: int) -> dict:
        resp = self._client.post(
            f"{self._base}/virtualization/virtual-machines/",
            json={
                "name": name,
                "device": device_id,
                "vcpus": vcpus,
                "memory": memory_mb,
                "disk": disk_gb * 1024,  # NetBox stores disk in MB
                "status": "active",
            },
        )
        resp.raise_for_status()
        return resp.json()

    def create_interface(self, vm_id: int, name: str = "eth0") -> dict:
        resp = self._client.post(
            f"{self._base}/virtualization/interfaces/",
            json={"virtual_machine": vm_id, "name": name},
        )
        resp.raise_for_status()
        return resp.json()

    def create_ip_address(self, address: str, dns_name: str, interface_id: int) -> dict:
        """Create an IP and assign it to a VM interface."""
        resp = self._client.post(
            f"{self._base}/ipam/ip-addresses/",
            json={
                "address": address,
                "dns_name": dns_name,
                "status": "active",
                "assigned_object_type": "virtualization.vminterface",
                "assigned_object_id": interface_id,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def set_primary_ip(self, vm_id: int, ip_id: int):
        resp = self._client.patch(
            f"{self._base}/virtualization/virtual-machines/{vm_id}/",
            json={"primary_ip4": ip_id},
        )
        resp.raise_for_status()
        return resp.json()
