import httpx


class NPMPlusClient:
    def __init__(self, url: str, email: str, password: str):
        self._base = url.rstrip("/")
        self._client = httpx.Client(timeout=12, verify=False, follow_redirects=True)
        self._token = self._login(email, password)

    def _login(self, email: str, password: str) -> str:
        resp = self._client.post(
            f"{self._base}/api/tokens",
            json={"identity": email, "secret": password},
        )
        resp.raise_for_status()
        data = resp.json()
        # Standard NPM returns {"token": "..."}
        if "token" in data:
            return data["token"]
        if "data" in data and "token" in data["data"]:
            return data["data"]["token"]
        # NPMplus uses cookie-based auth — the session cookie is stored automatically
        # by the httpx.Client; no explicit token needed
        return ""

    def close(self):
        self._client.close()

    @property
    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def find_certificate_id(self, search: str) -> int | None:
        """Find a certificate ID by matching search string against nice_name or domain_names."""
        resp = self._client.get(
            f"{self._base}/api/nginx/certificates",
            headers=self._auth_headers,
        )
        resp.raise_for_status()
        certs = resp.json()
        if isinstance(certs, dict) and "data" in certs:
            certs = certs["data"]
        search_lower = search.lower()
        for cert in certs:
            if search_lower in cert.get("nice_name", "").lower():
                return cert["id"]
            if any(search_lower in d.lower() for d in cert.get("domain_names", [])):
                return cert["id"]
        return None

    def create_proxy_host(
        self,
        domain_names: list[str],
        forward_host: str,
        forward_port: int,
        forward_scheme: str = "http",
        ssl_forced: bool = False,
        block_exploits: bool = True,
        certificate_id: int | None = None,
    ) -> dict:
        body: dict = {
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
        }
        if certificate_id is not None:
            body["certificate_id"] = certificate_id
        resp = self._client.post(
            f"{self._base}/api/nginx/proxy-hosts",
            headers=self._auth_headers,
            json=body,
        )
        resp.raise_for_status()
        return resp.json()
