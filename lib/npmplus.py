import httpx


class NPMPlusClient:
    def __init__(self, url: str, email: str, password: str):
        self._base = url.rstrip("/")
        self._client = httpx.Client(timeout=12)
        self._token = self._login(email, password)

    def _login(self, email: str, password: str) -> str:
        resp = self._client.post(
            f"{self._base}/api/tokens",
            json={"identity": email, "secret": password},
        )
        resp.raise_for_status()
        return resp.json()["token"]

    def close(self):
        self._client.close()

    def create_proxy_host(
        self,
        domain_names: list[str],
        forward_host: str,
        forward_port: int,
        forward_scheme: str = "http",
        ssl_forced: bool = False,
        block_exploits: bool = True,
    ) -> dict:
        resp = self._client.post(
            f"{self._base}/api/nginx/proxy-hosts",
            headers={"Authorization": f"Bearer {self._token}"},
            json={
                "domain_names": domain_names,
                "forward_scheme": forward_scheme,
                "forward_host": forward_host,
                "forward_port": forward_port,
                "ssl_forced": ssl_forced,
                "block_exploits": block_exploits,
                "caching_enabled": False,
                "allow_websocket_upgrade": True,
                "http2_support": False,
                "advanced_config": "",
                "locations": [],
                "meta": {"letsencrypt_agree": False, "dns_challenge": False},
            },
        )
        resp.raise_for_status()
        return resp.json()
