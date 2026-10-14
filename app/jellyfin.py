"""Read-only Jellyfin client.

This class deliberately has no method that sends anything other than GET.
Step 1 of the app only reads; when editing is added, the write calls will be
collection-scoped (POST/DELETE /Collections/{id}/Items) and nothing here will
ever be able to reach DELETE /Items, which deletes media from the filesystem.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

PAGE_SIZE = int(os.environ.get("JF_PAGE_SIZE", "500"))


class JellyfinError(Exception):
    pass


def nid(value: Any) -> str:
    """Normalise an item ID (Jellyfin shows them with or without dashes)."""
    return str(value).replace("-", "").lower()


class JellyfinReader:
    def __init__(self, url: str, api_key: str, verify_tls: bool = True,
                 user_id: str = "", timeout: float = 30.0):
        self._user_id = user_id
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={
                "Authorization": f'MediaBrowser Token="{api_key}"',
                "Accept": "application/json",
            },
            verify=verify_tls,
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        try:
            resp = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise JellyfinError(f"GET {path} failed: {type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            body = resp.text.strip().replace("\n", " ")[:200]
            raise JellyfinError(f"GET {path} -> HTTP {resp.status_code}: {body}")
        try:
            return resp.json()
        except ValueError as exc:
            raise JellyfinError(f"GET {path} returned non-JSON content") from exc

    def _user_params(self) -> dict:
        return {"userId": self._user_id} if self._user_id else {}

    async def _items(self, params: dict) -> list[dict]:
        out: list[dict] = []
        start = 0
        while True:
            page = dict(params, **self._user_params(), startIndex=start, limit=PAGE_SIZE,
                        enableTotalRecordCount="true")
            data = await self._get("/Items", page)
            items = data.get("Items", []) if isinstance(data, dict) else []
            out.extend(items)
            start += len(items)
            total = data.get("TotalRecordCount") if isinstance(data, dict) else None
            if not items:
                break
            if total is not None:
                if start >= total:
                    break
            elif len(items) < PAGE_SIZE:
                break
        return out

    async def public_info(self) -> dict:
        return await self._get("/System/Info/Public")

    async def libraries(self) -> list[dict]:
        data = await self._get("/Library/VirtualFolders")
        return data if isinstance(data, list) else []

    async def series(self, library_id: str | None = None) -> list[dict]:
        params = {"includeItemTypes": "Series", "recursive": "true",
                  "sortBy": "SortName", "sortOrder": "Ascending"}
        if library_id:
            params["parentId"] = library_id
        return await self._items(params)

    async def collections(self) -> list[dict]:
        return await self._items({"includeItemTypes": "BoxSet", "recursive": "true"})

    async def collection_members(self, collection_id: str) -> list[dict]:
        return await self._items({"parentId": collection_id})

    async def item_collections(self, item_id: str) -> list[dict]:
        data = await self._get(f"/Items/{item_id}/Collections", self._user_params())
        if isinstance(data, dict):
            return data.get("Items", [])
        return data if isinstance(data, list) else []

    async def autocollections_export(self) -> dict:
        data = await self._get("/AutoCollections/ExportConfiguration")
        return data if isinstance(data, dict) else {}
