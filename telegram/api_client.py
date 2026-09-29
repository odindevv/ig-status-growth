"""
HTTP client for the 3 backend APIs.
Uses httpx for async HTTP requests.
"""

import httpx
from config import telegram_settings as settings
from logger import logger

_TIMEOUT = 30.0
_CHECK_TIMEOUT = 300.0  # Longer timeout for check operations


class APIClient:
    """Talks to ig_account_checker, ig_growth_api, ig_status_api via HTTP."""

    def __init__(self) -> None:
        self._clients: dict[str, httpx.AsyncClient] = {}

    async def _get_client(self, service: str) -> httpx.AsyncClient:
        if service not in self._clients:
            base_url = {
                "checker": f"{settings.api_base_url}/api/v1/checker",
                "growth": f"{settings.api_base_url}/api/v1/growth",
                "status": f"{settings.api_base_url}/api/v1/status",
            }[service]
            headers = {}
            if settings.api_key:
                headers["X-API-Key"] = settings.api_key
            self._clients[service] = httpx.AsyncClient(
                base_url=base_url,
                headers=headers,
                timeout=_TIMEOUT,
            )
        return self._clients[service]

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    # ── Generic helpers ───────────────────────────────────────

    async def get(self, service: str, path: str, **params) -> dict | list | None:
        client = await self._get_client(service)
        try:
            resp = await client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning(f"[api] {service} GET {path} → {e.response.status_code}")
            try:
                return e.response.json()
            except Exception:
                return {"error": str(e)}
        except Exception as e:
            logger.error(f"[api] {service} GET {path} failed: {e}")
            return {"error": str(e)}

    async def post(self, service: str, path: str, json: dict | None = None, timeout: float | None = None) -> dict | list | None:
        client = await self._get_client(service)
        try:
            resp = await client.post(path, json=json, timeout=timeout or _TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning(f"[api] {service} POST {path} → {e.response.status_code}")
            try:
                return e.response.json()
            except Exception:
                return {"error": str(e)}
        except Exception as e:
            logger.error(f"[api] {service} POST {path} failed: {e}")
            return {"error": str(e)}

    async def put(self, service: str, path: str, json: dict | None = None) -> dict | None:
        client = await self._get_client(service)
        try:
            resp = await client.put(path, json=json)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning(f"[api] {service} PUT {path} → {e.response.status_code}")
            try:
                return e.response.json()
            except Exception:
                return {"error": str(e)}
        except Exception as e:
            logger.error(f"[api] {service} PUT {path} failed: {e}")
            return {"error": str(e)}

    async def delete(self, service: str, path: str) -> dict | None:
        client = await self._get_client(service)
        try:
            resp = await client.delete(path)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning(f"[api] {service} DELETE {path} → {e.response.status_code}")
            try:
                return e.response.json()
            except Exception:
                return {"error": str(e)}
        except Exception as e:
            logger.error(f"[api] {service} DELETE {path} failed: {e}")
            return {"error": str(e)}

    # ── Health checks ─────────────────────────────────────────

    async def health(self, service: str) -> dict:
        """Check if an API is reachable."""
        try:
            data = await self.get(service, "/health")
            return {"ok": True, "data": data}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# Singleton
api = APIClient()
