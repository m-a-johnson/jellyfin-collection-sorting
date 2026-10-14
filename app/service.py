"""Builds the show/collection snapshot and runs the self-check."""
from __future__ import annotations

import asyncio
import time

from .jellyfin import HEX32, CollectionWriter, JellyfinError, JellyfinReader, nid, valid_collection_name

# Which kinds of title each Jellyfin library type can hold. Mixed-content libraries
# report an empty type, so they count as both. Music, photos, etc. are not listed.
LIBRARY_KINDS = {"tvshows": ["show"], "movies": ["movie"], "mixed": ["show", "movie"], "": ["show", "movie"]}
MEMBER_KINDS = {"Series": "show", "Movie": "movie"}


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
            kinds = LIBRARY_KINDS.get(ctype, [])
            libraries.append({
                "id": nid(lib["ItemId"]),
                "name": lib.get("Name", "?"),
                "type": ctype,
                "kinds": kinds,
                "is_show_library": "show" in kinds,
                "is_movie_library": "movie" in kinds,
            })

        by_kind: dict[str, dict[str, dict]] = {"show": {}, "movie": {}}
        for lib in libraries:
            for kind in lib["kinds"]:
                fetch = r.series if kind == "show" else r.movies
                for item in await fetch(lib["id"]):
                    sid = nid(item["Id"])
                    rec = by_kind[kind].setdefault(sid, {
                        "id": sid, "name": item.get("Name", "?"), "kind": kind,
                        "year": item.get("ProductionYear"),
                        "library_ids": [], "collection_ids": [],
                    })
                    if lib["id"] not in rec["library_ids"]:
                        rec["library_ids"].append(lib["id"])
        shows, movies = by_kind["show"], by_kind["movie"]
        titles = {**shows, **movies}

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
            title_members = []
            other = 0
            for m in members:
                kind = MEMBER_KINDS.get(m.get("Type"))
                if kind:
                    mid = nid(m["Id"])
                    title_members.append({"id": mid, "name": m.get("Name", "?"), "kind": kind,
                                          "year": m.get("ProductionYear")})
                    if mid in titles:
                        titles[mid]["collection_ids"].append(cid)
                else:
                    other += 1
            title_members.sort(key=lambda s: s["name"].lower())
            collections.append({
                "id": cid, "name": c.get("Name", "?"),
                "managed": c.get("Name", "").strip().lower() in managed,
                "members": title_members, "other_count": other,
            })
        collections.sort(key=lambda c: c["name"].lower())

        managed_ids = {c["id"] for c in collections if c["managed"]}
        show_list = sorted(shows.values(), key=lambda s: s["name"].lower())
        movie_list = sorted(movies.values(), key=lambda s: s["name"].lower())
        everything = show_list + movie_list
        uncollected = [s["id"] for s in everything if not s["collection_ids"]]
        uncollected_ignoring_managed = [
            s["id"] for s in everything
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
            "movies": movie_list,
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
            raise ApplyError("Invalid item id in request.")
        add_set, remove_set = set(add), set(remove)
        if add_set & remove_set:
            raise ApplyError("An item cannot be both added and removed in one request.")

        async with self._write_lock:
            snap = await self.snapshot(force=True)
            col = next((c for c in snap["collections"] if c["id"] == collection_id), None)
            if col is None:
                raise ApplyError("Collection not found. It may have been deleted; refresh.", 404)
            if col["managed"]:
                raise ApplyError(f'"{col["name"]}" is managed by the Auto Collections plugin, '
                                 "which would undo changes made here.", 409)
            known = {s["id"] for s in snap["shows"]} | {s["id"] for s in snap["movies"]}
            unknown = add_set - known
            if unknown:
                raise ApplyError("Some items to add were not found in any show or movie library; refresh.")

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
            names = {s["id"]: s["name"] for s in fresh["shows"] + fresh["movies"]}
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

    async def set_item_collections(self, item_id: str, add_to: list[str], remove_from: list[str],
                                   dry_run: bool = False) -> dict:
        """Put one show or movie into some collections and take it out of others.

        Everything is validated against a fresh read before the first write, so a request
        that names a managed or missing collection changes nothing. Writes then run one
        collection at a time and stop at the first failure, reporting what already went through.
        """
        if self._read_only:
            raise ApplyError("The app is running in read-only mode.", 403)
        if not HEX32.match(item_id) or any(not HEX32.match(c) for c in add_to + remove_from):
            raise ApplyError("Invalid id in request.")
        if set(add_to) & set(remove_from):
            raise ApplyError("A collection cannot be both added to and removed from in one request.")

        async with self._write_lock:
            snap = await self.snapshot(force=True)
            item = next((t for t in snap["shows"] + snap["movies"] if t["id"] == item_id), None)
            if item is None:
                raise ApplyError("That title was not found in any show or movie library; refresh.", 404)
            cols = {c["id"]: c for c in snap["collections"]}
            missing = [c for c in add_to + remove_from if c not in cols]
            if missing:
                raise ApplyError("A collection was not found. It may have been deleted; refresh.", 404)
            managed = [cols[c]["name"] for c in add_to + remove_from if cols[c]["managed"]]
            if managed:
                raise ApplyError(f'Managed by the Auto Collections plugin, which would undo changes: '
                                 f'{", ".join(managed)}.', 409)

            def has(cid: str) -> bool:
                return any(m["id"] == item_id for m in cols[cid]["members"])

            to_add = [c for c in add_to if not has(c)]
            to_remove = [c for c in remove_from if has(c)]
            result = {
                "id": item_id, "name": item["name"], "dry_run": dry_run,
                "added_to": [cols[c]["name"] for c in to_add],
                "removed_from": [cols[c]["name"] for c in to_remove],
                "skipped": [cols[c]["name"] for c in add_to if c not in to_add]
                           + [cols[c]["name"] for c in remove_from if c not in to_remove],
                "applied": False, "verified": None, "mismatches": [],
            }
            if dry_run or not (to_add or to_remove):
                return result

            done: list[str] = []
            try:
                for cid in to_add:
                    await self._writer.add(cid, [item_id])
                    done.append(f'added to "{cols[cid]["name"]}"')
                for cid in to_remove:
                    await self._writer.remove(cid, [item_id])
                    done.append(f'removed from "{cols[cid]["name"]}"')
            except JellyfinError as exc:
                self._cached = None
                did = "; ".join(done) if done else "nothing was changed"
                raise ApplyError(f"Partly applied ({did}) before Jellyfin refused: {exc}. "
                                 "Refresh to see the current state.", 502) from exc
            except ValueError as exc:  # defensive: the writer's own validation
                self._cached = None
                raise ApplyError(f"Refused to send: {exc}", 400) from exc

            result["applied"] = True
            fresh = await self.snapshot(force=True)
            fresh_cols = {c["id"]: c for c in fresh["collections"]}

            def now_in(cid: str) -> bool:
                c = fresh_cols.get(cid)
                return bool(c) and any(m["id"] == item_id for m in c["members"])

            mismatches = [{"collection": cols[c]["name"], "expected": "in"} for c in to_add if not now_in(c)]
            mismatches += [{"collection": cols[c]["name"], "expected": "out"} for c in to_remove if now_in(c)]
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
        shows, movies, cols = snap["shows"], snap["movies"], snap["collections"]
        title_libs = [x for x in snap["libraries"] if x["kinds"]]
        add("API key and library snapshot", "pass",
            f"{len(title_libs)} show/movie libraries, {len(shows)} shows, {len(movies)} movies, "
            f"{len(cols)} collections")
        if not title_libs:
            add("Show or movie libraries found", "warn",
                "No library of type tvshows, movies or mixed was listed.")
        for w in snap["warnings"]:
            add("Auto Collections export", "warn", w)

        titles = shows + movies
        if titles:
            with_year = sum(1 for s in titles if s["year"])
            add("Production years present", "pass" if with_year else "warn",
                f"{with_year} of {len(titles)} shows and movies have a production year")

        if cols and not any(c["members"] or c["other_count"] for c in cols):
            add("Collection members readable", "warn",
                "Collections exist but none returned members via /Items?parentId=. "
                "The member lookup may work differently in 12.1.")
        elif cols:
            add("Collection members readable", "pass",
                f"{sum(len(c['members']) for c in cols)} show/movie memberships across {len(cols)} collections")

        collected = [s for s in titles if s["collection_ids"]]
        uncollected = [s for s in titles if not s["collection_ids"]]
        picks = collected[: sample // 2 + sample % 2] + uncollected[: sample // 2]
        if not picks:
            add("Native per-show lookup cross-check", "warn", "No shows or movies available to sample.")
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
                f"{len(mismatches)} of {len(picks)} sampled titles disagree. "
                + " | ".join(mismatches[:5]))
        else:
            add("Native per-show lookup cross-check", "pass",
                f"{len(picks)} sampled titles agree between the member lists and /Items/{{id}}/Collections")
        return checks
