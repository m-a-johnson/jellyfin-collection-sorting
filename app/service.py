"""Builds the show/collection snapshot and runs the self-check."""
from __future__ import annotations

import asyncio
import time

from .jellyfin import HEX32, CollectionWriter, JellyfinError, JellyfinReader, nid, valid_collection_name

SHOW_LIBRARY_TYPES = {"tvshows", "mixed", ""}


class ApplyError(Exception):
    """A change request that was refused or only partly carried out."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _managed_names(export: dict) -> set[str]:
    """Collection names owned by the Auto Collections plugin.

    TitleMatchPairs[].CollectionName is seen in a real export. The shape of
    ExpressionCollections entries has not been seen (the list was empty), so
    this tries the likely keys and ignores anything else.
    """
    names: set[str] = set()
    for pair in export.get("TitleMatchPairs") or []:
        if isinstance(pair, dict) and pair.get("CollectionName"):
            names.add(str(pair["CollectionName"]).strip().lower())
    for entry in export.get("ExpressionCollections") or []:
        if isinstance(entry, dict):
            for key in ("CollectionName", "Name"):
                if entry.get(key):
                    names.add(str(entry[key]).strip().lower())
                    break
    return names


class Service:
    def __init__(self, reader: JellyfinReader, writer: CollectionWriter,
                 cache_seconds: int, read_only: bool = False):
        self._reader = reader
        self._writer = writer
        self._ttl = cache_seconds
        self._read_only = read_only
        self._lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._cached: dict | None = None

    async def snapshot(self, force: bool = False) -> dict:
        async with self._lock:
            fresh = self._cached and (time.time() - self._cached["fetched_at"] < self._ttl)
            if force or not fresh:
                self._cached = await self._build()
            return self._cached

    async def _build(self) -> dict:
        r = self._reader
        warnings: list[str] = []

        libs_raw = await r.libraries()
        libraries = []
        for lib in libs_raw:
            ctype = lib.get("CollectionType") or ""
            if ctype == "boxsets":
                continue
            libraries.append({
                "id": nid(lib["ItemId"]),
                "name": lib.get("Name", "?"),
                "type": ctype,
                "is_show_library": ctype in SHOW_LIBRARY_TYPES,
            })

        shows: dict[str, dict] = {}
        for lib in libraries:
            if not lib["is_show_library"]:
                continue
            for item in await r.series(lib["id"]):
                sid = nid(item["Id"])
                show = shows.setdefault(sid, {
                    "id": sid, "name": item.get("Name", "?"),
                    "year": item.get("ProductionYear"),
                    "library_ids": [], "collection_ids": [],
                })
                if lib["id"] not in show["library_ids"]:
                    show["library_ids"].append(lib["id"])

        try:
            managed = _managed_names(await r.autocollections_export())
        except JellyfinError as exc:
            managed = set()
            warnings.append(f"Auto Collections export unavailable ({exc}); "
                            "no collection is flagged as plugin-managed.")

        collections_raw = await r.collections()
        sem = asyncio.Semaphore(8)

        async def members_of(c: dict) -> list[dict]:
            async with sem:
                return await r.collection_members(c["Id"])

        member_lists = await asyncio.gather(*(members_of(c) for c in collections_raw))

        collections = []
        for c, members in zip(collections_raw, member_lists):
            cid = nid(c["Id"])
            series_members = []
            other = 0
            for m in members:
                if m.get("Type") == "Series":
                    mid = nid(m["Id"])
                    series_members.append({"id": mid, "name": m.get("Name", "?"),
                                           "year": m.get("ProductionYear")})
                    if mid in shows:
                        shows[mid]["collection_ids"].append(cid)
                else:
                    other += 1
            series_members.sort(key=lambda s: s["name"].lower())
            collections.append({
                "id": cid, "name": c.get("Name", "?"),
                "managed": c.get("Name", "").strip().lower() in managed,
                "members": series_members, "other_count": other,
            })
        collections.sort(key=lambda c: c["name"].lower())

        managed_ids = {c["id"] for c in collections if c["managed"]}
        show_list = sorted(shows.values(), key=lambda s: s["name"].lower())
        uncollected = [s["id"] for s in show_list if not s["collection_ids"]]
        uncollected_ignoring_managed = [
            s["id"] for s in show_list
            if not [cid for cid in s["collection_ids"] if cid not in managed_ids]
        ]

        info = await r.public_info()
        return {
            "fetched_at": time.time(),
            "read_only": self._read_only,
            "managed_names": sorted(managed),
            "server": {"name": info.get("ServerName"), "version": info.get("Version")},
            "libraries": libraries,
            "collections": collections,
            "shows": show_list,
            "uncollected_ids": uncollected,
            "uncollected_ids_ignoring_managed": uncollected_ignoring_managed,
            "warnings": warnings,
        }

    async def create_collection(self, name: str, dry_run: bool = False) -> dict:
        """Create an empty collection, then read Jellyfin back to confirm it exists."""
        if self._read_only:
            raise ApplyError("The app is running in read-only mode.", 403)
        name = (name or "").strip()
        if not valid_collection_name(name):
            raise ApplyError("A collection name must be 1 to 100 characters with no control characters.")
        lowered = name.lower()

        async with self._write_lock:
            snap = await self.snapshot(force=True)
            if any(c["name"].strip().lower() == lowered for c in snap["collections"]):
                raise ApplyError(f'A collection named "{name}" already exists.', 409)
            if lowered in snap["managed_names"]:
                raise ApplyError(f'"{name}" is a collection name used by an Auto Collections rule, '
                                 "so the plugin would fill it. Pick another name.", 409)
            result = {"name": name, "dry_run": dry_run, "created": False, "id": None, "verified": None}
            if dry_run:
                return result
            try:
                new_id = await self._writer.create(name)
            except JellyfinError as exc:
                self._cached = None
                raise ApplyError(f"Jellyfin refused to create the collection: {exc}", 502) from exc

            result["created"] = True
            result["id"] = new_id
            try:
                fresh = await self.snapshot(force=True)
            except JellyfinError:
                result["verified"] = False
                return result
            found = (next((c for c in fresh["collections"] if new_id and c["id"] == new_id), None)
                     or next((c for c in fresh["collections"] if c["name"].strip().lower() == lowered), None))
            result["id"] = found["id"] if found else new_id
            result["verified"] = found is not None
            return result

    async def apply_changes(self, collection_id: str, add: list[str], remove: list[str],
                            dry_run: bool = False) -> dict:
        """Add and remove shows in one collection, then read it back to verify.

        Everything is re-checked against a fresh read of Jellyfin, so a stale
        browser page cannot cause a surprise: adds of shows that are already
        members and removals of shows that are not members are skipped and
        reported rather than sent.
        """
        if self._read_only:
            raise ApplyError("The app is running in read-only mode.", 403)
        if not HEX32.match(collection_id):
            raise ApplyError("Invalid collection id.")
        bad = [i for i in add + remove if not HEX32.match(i)]
        if bad:
            raise ApplyError("Invalid show id in request.")
        add_set, remove_set = set(add), set(remove)
        if add_set & remove_set:
            raise ApplyError("A show cannot be both added and removed in one request.")

        async with self._write_lock:
            snap = await self.snapshot(force=True)
            col = next((c for c in snap["collections"] if c["id"] == collection_id), None)
            if col is None:
                raise ApplyError("Collection not found. It may have been deleted; refresh.", 404)
            if col["managed"]:
                raise ApplyError(f'"{col["name"]}" is managed by the Auto Collections plugin, '
                                 "which would undo changes made here.", 409)
            known = {s["id"] for s in snap["shows"]}
            unknown = add_set - known
            if unknown:
                raise ApplyError("Some shows to add were not found in any show library; refresh.")

            members = {m["id"] for m in col["members"]}
            to_add = sorted(add_set - members)
            to_remove = sorted(remove_set & members)
            result = {
                "collection": col["name"], "dry_run": dry_run,
                "added": to_add, "removed": to_remove,
                "skipped_add": sorted(add_set & members),
                "skipped_remove": sorted(remove_set - members),
                "applied": False, "verified": None, "mismatches": [],
            }
            if dry_run or not (to_add or to_remove):
                return result

            done_add = done_remove = 0
            try:
                for i in range(0, len(to_add), 50):
                    chunk = to_add[i:i + 50]
                    await self._writer.add(collection_id, chunk)
                    done_add += len(chunk)
                for i in range(0, len(to_remove), 50):
                    chunk = to_remove[i:i + 50]
                    await self._writer.remove(collection_id, chunk)
                    done_remove += len(chunk)
            except JellyfinError as exc:
                self._cached = None
                raise ApplyError(
                    f"Partly applied: {done_add} of {len(to_add)} added and {done_remove} of "
                    f"{len(to_remove)} removed before Jellyfin refused: {exc}. "
                    "Refresh to see the current state.", 502) from exc
            except ValueError as exc:  # defensive: the writer's own validation
                self._cached = None
                raise ApplyError(f"Refused to send: {exc}", 400) from exc

            result["applied"] = True
            fresh = await self.snapshot(force=True)
            fresh_col = next((c for c in fresh["collections"] if c["id"] == collection_id), None)
            names = {s["id"]: s["name"] for s in fresh["shows"]}
            fresh_members = {m["id"] for m in fresh_col["members"]} if fresh_col else set()
            mismatches = []
            for sid in to_add:
                if sid not in fresh_members:
                    mismatches.append({"id": sid, "name": names.get(sid, sid), "expected": "in"})
            for sid in to_remove:
                if sid in fresh_members:
                    mismatches.append({"id": sid, "name": names.get(sid, sid), "expected": "out"})
            result["mismatches"] = mismatches
            result["verified"] = not mismatches
            return result

    async def selfcheck(self, sample: int = 12) -> list[dict]:
        checks: list[dict] = []

        def add(name: str, status: str, detail: str) -> None:
            checks.append({"name": name, "status": status, "detail": detail})

        r = self._reader
        try:
            info = await r.public_info()
        except JellyfinError as exc:
            add("Server reachable", "fail", str(exc))
            return checks
        version = str(info.get("Version", "?"))
        add("Server reachable", "pass", f"{info.get('ServerName', '?')} {version}")
        if not version.startswith("12."):
            add("Server version", "warn",
                f"Expected 12.x, got {version}. Endpoint behaviour was only checked "
                "against the 12.1.0 spec.")

        try:
            snap = await self.snapshot(force=True)
        except JellyfinError as exc:
            add("API key and library snapshot", "fail", str(exc))
            return checks
        shows, cols = snap["shows"], snap["collections"]
        show_libs = [x for x in snap["libraries"] if x["is_show_library"]]
        add("API key and library snapshot", "pass",
            f"{len(show_libs)} show libraries, {len(shows)} shows, {len(cols)} collections")
        if not show_libs:
            add("Show libraries found", "warn", "No library of type tvshows/mixed was listed.")
        for w in snap["warnings"]:
            add("Auto Collections export", "warn", w)

        if shows:
            with_year = sum(1 for s in shows if s["year"])
            add("Show years present", "pass" if with_year else "warn",
                f"{with_year} of {len(shows)} shows have a production year")

        if cols and not any(c["members"] or c["other_count"] for c in cols):
            add("Collection members readable", "warn",
                "Collections exist but none returned members via /Items?parentId=. "
                "The member lookup may work differently in 12.1.")
        elif cols:
            add("Collection members readable", "pass",
                f"{sum(len(c['members']) for c in cols)} show memberships across {len(cols)} collections")

        collected = [s for s in shows if s["collection_ids"]]
        uncollected = [s for s in shows if not s["collection_ids"]]
        picks = collected[: sample // 2 + sample % 2] + uncollected[: sample // 2]
        if not picks:
            add("Native per-show lookup cross-check", "warn", "No shows available to sample.")
            return checks

        mismatches, errors = [], []
        for show in picks:
            try:
                native = {nid(c["Id"]) for c in await r.item_collections(show["id"])}
            except JellyfinError as exc:
                errors.append(str(exc))
                break
            if native != set(show["collection_ids"]):
                mismatches.append(f"{show['name']}: map={sorted(show['collection_ids'])} "
                                  f"native={sorted(native)}")
        if errors:
            add("Native per-show lookup cross-check", "warn",
                f"/Items/{{id}}/Collections was not usable ({errors[0]}). "
                "Relying on the inverted collection member lists only.")
        elif mismatches:
            add("Native per-show lookup cross-check", "fail",
                f"{len(mismatches)} of {len(picks)} sampled shows disagree. "
                + " | ".join(mismatches[:5]))
        else:
            add("Native per-show lookup cross-check", "pass",
                f"{len(picks)} sampled shows agree between the member lists and /Items/{{id}}/Collections")
        return checks
