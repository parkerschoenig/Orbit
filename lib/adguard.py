import httpx


class AdGuardClient:
    def __init__(self, url: str, username: str, password: str):
        self._base = url.rstrip("/")
        self._auth = (username, password)
        self._client = httpx.Client(timeout=10)

    def close(self):
        self._client.close()

    def add_rewrite(self, domain: str, answer: str):
        """Create a DNS rewrite rule: domain → answer (IP)."""
        resp = self._client.post(
            f"{self._base}/control/rewrite/add",
            json={"domain": domain, "answer": answer},
            auth=self._auth,
        )
        resp.raise_for_status()

    def list_rewrites(self) -> list[dict]:
        resp = self._client.get(f"{self._base}/control/rewrite/list", auth=self._auth)
        resp.raise_for_status()
        return resp.json()

    def rewrite_exists(self, domain: str) -> bool:
        return any(r["domain"] == domain for r in self.list_rewrites())
