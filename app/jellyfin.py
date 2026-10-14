"""Jellyfin clients.

Two classes, split on purpose:

* JellyfinReader sends GET requests only. It has no way to change anything.
* CollectionWriter is the only code that can change anything, and it can only
  send three kinds of request: POST /Collections (create an empty collection),
  and POST or DELETE /Collections/{32-hex id}/Items (add or remove membership,
  which never touches the media). It cannot reach DELETE /Items, which deletes
  media from the filesystem: the method, the path and every ID are validated
  before any request is built.
"""
from __future__ import annotations

import os
import re
from typing import Any

import httpx

PAGE_SIZE = int(os.environ.get("JF_PAGE_SIZE", "500"))
WRITE_CHUNK = max(1, int(os.environ.get("JF_WRITE_CHUNK", "50")))
HEX32 = re.compile(r"^[0-9a-f]{32}$")


class JellyfinError(Exception):
    pass


def nid(value: Any) -> str:
    """Normalise an item ID (Jellyfin shows them with or without dashes)."""
    return str(value).replace("-", "").lower()


def valid_collection_name(name: Any) -> bool:
    """1 to 100 characters, already trimmed, no control characters."""
    return (isinstance(name, str) and name == name.strip() and 1 <= len(name) <= 100
            and not any(ord(c) < 32 or ord(c) == 127 for c in name))


def _make_client(url: str, api_key: str, verify_tls: bool, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=url.rstrip("/"),
        headers={
            "Authorization": f'MediaBrowser Token="{api_key}"',
            "Accept": "application/json",
        },
        verify=verify_tls,
        timeout=timeout,
    )


class CollectionWriter:
    """Adds items to, and removes items from, a collection. Nothing else."""

    def __init__(self, url: str, api_key: str, verify_tls: bool = True, timeout: float = 30.0):
        self._client = _make_client(url, api_key, verify_tls, timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def _send(self, method: str, collection_id: str, ids: list[str]) -> None:
        if method not in ("POST", "DELETE"):
            raise ValueError(f"method not allowed: {method}")
        if not HEX32.match(collection_id):
            raise ValueError("collection id must be 32 hex characters")
        if any(not HEX32.match(i) for i in ids):
            raise ValueError("item ids must be 32 hex characters")
        if not ids:
            return
        path = f"/Collections/{collection_id}/Items"
        try:
            resp = await self._client.request(method, path, params={"ids": ",".join(ids)})
        except httpx.HTTPError as exc:
            raise JellyfinError(f"{method} {path} failed: {type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            body = resp.text.strip().replace("\n", " ")[:200]
            raise JellyfinError(f"{method} {path} -> HTTP {resp.status_code}: {body}")

    async def create(self, name: str) -> str | None:
        """Create an empty collection. Returns its id, or None if Jellyfin does not say."""
        if not valid_collection_name(name):
            raise ValueError("invalid collection name")
        try:
            resp = await self._client.request("POST", "/Collections", params={"name": name})
        except httpx.HTTPError as exc:
            raise JellyfinError(f"POST /Collections failed: {type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            body = resp.text.strip().replace("\n", " ")[:200]
            raise JellyfinError(f"POST /Collections -> HTTP {resp.status_code}: {body}")
        try:
            data = resp.json()
        except ValueError:
            return None
        raw = data.get("Id") if isinstance(data, dict) else None
        return nid(raw) if raw else None

    async def add(self, collection_id: str, ids: list[str]) -> None:
        for i in range(0, len(ids), WRITE_CHUNK):
            await self._send("POST", collection_id, ids[i:i + WRITE_CHUNK])

    async def remove(self, collection_id: str, ids: list[str]) -> None:
        for i in range(0, len(ids), WRITE_CHUNK):
            await self._send("DELETE", collection_id, ids[i:i + WRITE_CHUNK])


class JellyfinReader:
    def __init__(self, url: str, api_key: str, verify_tls: bool = True,
                 user_id: str = "", timeout: float = 30.0):
        self._user_id = user_id
        self._client = _make_client(url, api_key, verify_tls, timeout)

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

    async def _titles(self, item_type: str, library_id: str | None) -> list[dict]:
        params = {"includeItemTypes": item_type, "recursive": "true",
                  "sortBy": "SortName", "sortOrder": "Ascending"}
        if library_id:
            params["parentId"] = library_id
        return await self._items(params)

    async def series(self, library_id: str | None = None) -> list[dict]:
        return await self._titles("Series", library_id)

    async def movies(self, library_id: str | None = None) -> list[dict]:
        return await self._titles("Movie", library_id)

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
