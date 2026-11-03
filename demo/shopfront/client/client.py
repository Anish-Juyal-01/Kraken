import httpx


class ShopfrontClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8080") -> None:
        self._http = httpx.Client(base_url=base_url, timeout=10)

    def get_user(self, user_id: int, version: str = "v2") -> dict:
        resp = self._http.get(f"/{version}/users/{user_id}")
        resp.raise_for_status()
        return resp.json()

    def reset_password(self) -> bool:
        return self._http.post("/auth/reset").status_code == 200
