"""End-to-end checks against the mock server. Run from the project root:

    python tests/run_tests.py
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MOCK_PORT, APP_PORT = 18096, 18099
failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


def wait_up(url: str, timeout: float = 15) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            httpx.get(url, timeout=1)
            return True
        except httpx.HTTPError:
            time.sleep(0.2)
    return False


def start(args: list[str], port: int, probe: str, env_extra: dict) -> subprocess.Popen:
    env = dict(os.environ, **env_extra)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", *args, "--port", str(port), "--log-level", "warning"],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if not wait_up(f"http://127.0.0.1:{port}{probe}"):
        proc.terminate()
        print(proc.communicate()[0])
        raise SystemExit(f"{args[0]} did not start")
    return proc


def stop(*procs):
    for p in procs:
        p.terminate()
        p.wait(timeout=10)


def scenario(mock_env: dict, app_env: dict | None = None):
    mock = start(["tests.mock_jellyfin:app"], MOCK_PORT, "/System/Info/Public", mock_env)
    cfg = Path(tempfile.mkdtemp()) / "config.yml"
    env = {"JELLYFIN_URL": f"http://127.0.0.1:{MOCK_PORT}", "JELLYFIN_API_KEY": "testkey",
           "CONFIG_PATH": str(cfg), "JF_PAGE_SIZE": "3", "CACHE_SECONDS": "0"}
    env.update(app_env or {})
    app = start(["app.main:app"], APP_PORT, "/", env)
    return mock, app


base = f"http://127.0.0.1:{APP_PORT}"


def config_tests():
    print("config")
    from app.config import ConfigError, PLACEHOLDER_KEY, load_config
    for k in ("JELLYFIN_URL", "JELLYFIN_API_KEY", "CONFIG_PATH"):
        os.environ.pop(k, None)
    d = Path(tempfile.mkdtemp())
    p = d / "sub" / "config.yml"
    try:
        load_config(p)
        check("missing file raises", False)
    except ConfigError as e:
        check("missing file writes a template and raises", p.exists() and "template" in str(e), str(e))
        check("template is chmod 600", (p.stat().st_mode & 0o777) == 0o600, oct(p.stat().st_mode))
    try:
        load_config(p)
        check("placeholder template is rejected", False)
    except ConfigError as e:
        check("placeholder template is rejected", "real address" in str(e) or "api_key" in str(e), str(e))
    p.write_text('jellyfin:\n  url: "https://jelly.test"\n  api_key: 1234567890\n')
    try:
        load_config(p)
        check("unquoted numeric key rejected", False)
    except ConfigError as e:
        check("unquoted numeric key rejected", "quoted string" in str(e), str(e))
    p.write_text(f'jellyfin:\n  url: "https://jelly.test/"\n  api_key: "abc123"\n  verify_tls: false\napp:\n  cache_seconds: 5\n')
    c = load_config(p)
    check("valid file parsed", (c.url, c.api_key, c.verify_tls, c.cache_seconds) == ("https://jelly.test", "abc123", False, 5), str(c))
    os.environ["JELLYFIN_API_KEY"] = "fromenv"
    check("env overrides file", load_config(p).api_key == "fromenv")
    os.environ.pop("JELLYFIN_API_KEY")


def happy_path():
    print("happy path (page size 3, dashed vs dashless IDs)")
    mock, app = scenario({})
    try:
        r = httpx.get(f"{base}/api/snapshot")
        check("snapshot 200", r.status_code == 200, r.text[:200])
        s = r.json()
        check("server info", s["server"] == {"name": "MockJelly", "version": "12.1.0"}, str(s["server"]))
        check("boxsets library excluded, show libs flagged",
              [l["name"] for l in s["libraries"]] == ["Kids Shows", "Adult Shows", "Movies"]
              and [l["is_show_library"] for l in s["libraries"]] == [True, True, False], str(s["libraries"]))
        check("library IDs have no dashes", all("-" not in l["id"] for l in s["libraries"]))
        check("all 9 shows found across pages", len(s["shows"]) == 9, str(len(s["shows"])))
        names = [(x["name"], x["year"]) for x in s["shows"]]
        check("duplicate titles kept apart by year", ("Reboot", 1994) in names and ("Reboot", 2022) in names)
        cols = {c["name"]: c for c in s["collections"]}
        check("collections sorted", [c["name"] for c in s["collections"]] ==
              ["Empty Test", "ETV Discovery", "Evening Lineup", "Star Wars Collection"], str(list(cols)))
        etv = cols["ETV Discovery"]
        check("ETV has 3 shows and 1 movie, no unknown items",
              [m["kind"] for m in etv["members"]].count("show") == 3 and [m["kind"] for m in etv["members"]].count("movie") == 1
              and etv["other_count"] == 0, str(etv))
        check("movie libraries and kinds reported",
              [(l["name"], l["kinds"]) for l in s["libraries"]] ==
              [("Kids Shows", ["show"]), ("Adult Shows", ["show"]), ("Movies", ["movie"])], str(s["libraries"]))
        check("4 movies found", sorted(m["name"] for m in s["movies"]) == ["Encanto", "Moana", "Paddington", "Some Movie"]
              and all(m["kind"] == "movie" for m in s["movies"]), str(s["movies"]))
        check("movie collection membership tracked",
              next(m for m in s["movies"] if m["name"] == "Some Movie")["collection_ids"] == [etv["id"]])
        check("uncollected includes movies", {m["name"] for m in s["movies"] if m["id"] in set(s["uncollected_ids"])}
              == {"Moana", "Encanto", "Paddington"})
        from app.service import LIBRARY_KINDS
        check("mixed and untyped libraries hold both kinds", LIBRARY_KINDS["mixed"] == LIBRARY_KINDS[""] == ["show", "movie"]
              and "musicvideos" not in LIBRARY_KINDS and "music" not in LIBRARY_KINDS)
        check("only Star Wars flagged managed", [n for n, c in cols.items() if c["managed"]] == ["Star Wars Collection"])
        by_name = {(x["name"], x["year"]): x for x in s["shows"]}
        octo = by_name[("Octonauts", 2010)]
        check("Octonauts in 2 collections", sorted(octo["collection_ids"]) ==
              sorted([etv["id"], cols["Evening Lineup"]["id"]]), str(octo))
        unc = sorted(x["name"] + str(x["year"]) for x in s["shows"] if x["id"] in set(s["uncollected_ids"]))
        check("uncollected = 4 expected shows", unc == ["Andor2022", "Bluey2018", "Daniel Tiger2012", "Reboot2022"], str(unc))
        unc2 = [x["name"] for x in s["shows"] if x["id"] in set(s["uncollected_ids_ignoring_managed"])]
        check("ignoring managed adds Star Wars Rebels", len(unc2) == 5 and "Star Wars Rebels" in unc2, str(unc2))
        check("no warnings", s["warnings"] == [], str(s["warnings"]))

        sc = httpx.get(f"{base}/api/selfcheck").json()["checks"]
        bad = [c for c in sc if c["status"] != "pass"]
        check("self-check all pass", not bad, str(bad))
        check("native cross-check sampled 12 titles", any("12 sampled titles agree" in c["detail"] for c in sc), str(sc))
        page = httpx.get(f"{base}/")
        check("frontend served", page.status_code == 200 and "Collection Manager" in page.text)
    finally:
        stop(app, mock)


def native_disagrees():
    print("scenario: native endpoint disagrees with member lists")
    mock, app = scenario({"MOCK_BREAK_NATIVE": "1"})
    try:
        sc = httpx.get(f"{base}/api/selfcheck").json()["checks"]
        cross = [c for c in sc if "cross-check" in c["name"]]
        check("cross-check fails", cross and cross[0]["status"] == "fail", str(sc))
    finally:
        stop(app, mock)


def native_missing():
    print("scenario: native endpoint 404, Auto Collections export 404")
    mock, app = scenario({"MOCK_NO_NATIVE": "1", "MOCK_NO_AUTOCOL": "1"})
    try:
        s = httpx.get(f"{base}/api/snapshot").json()
        check("snapshot still builds", len(s["shows"]) == 9)
        check("warning about missing export", len(s["warnings"]) == 1 and "Auto Collections" in s["warnings"][0], str(s["warnings"]))
        check("nothing flagged managed", not any(c["managed"] for c in s["collections"]))
        sc = httpx.get(f"{base}/api/selfcheck").json()["checks"]
        cross = [c for c in sc if "cross-check" in c["name"]]
        check("cross-check degrades to warn", cross and cross[0]["status"] == "warn", str(sc))
    finally:
        stop(app, mock)


def bad_key():
    print("scenario: wrong API key")
    mock, app = scenario({}, {"JELLYFIN_API_KEY": "wrongkey"})
    try:
        r = httpx.get(f"{base}/api/snapshot")
        check("502 with Jellyfin's status in message", r.status_code == 502 and "401" in r.json().get("error", ""), r.text[:200])
        check("key not echoed in error", "wrongkey" not in r.text)
        sc = httpx.get(f"{base}/api/selfcheck").json()["checks"]
        check("self-check reports failure", any(c["status"] == "fail" for c in sc), str(sc))
    finally:
        stop(app, mock)


def startup_without_config():
    print("startup with no config")
    d = Path(tempfile.mkdtemp())
    env = {k: v for k, v in os.environ.items() if not k.startswith("JELLYFIN_")}
    env["CONFIG_PATH"] = str(d / "config.yml")
    out = subprocess.run([sys.executable, "-c", "import app.main"], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=30)
    check("exits non-zero with a clear message", out.returncode == 1 and "Configuration problem" in out.stderr, out.stderr[-300:])
    check("template written", (d / "config.yml").exists())



# ---------------------------------------------------------------- editor / writes

HDR = {"X-Requested-With": "jf-collections"}
MOCK_AUTH = {"Authorization": 'MediaBrowser Token="testkey"'}


def sid(n: int) -> str:          # show ids used by the mock
    return ("b" + f"{n:02d}").ljust(32, "0")


BLUEY, OCTONAUTS, WILD_KRATTS, REBOOT_94, DANIEL_TIGER = sid(1), sid(2), sid(3), sid(4), sid(7)
MOVIE_SOME, MOANA, ENCANTO = [("c%02d" % n).ljust(32, "0") for n in (1, 2, 3)]


def mock_log() -> list:
    return httpx.get(f"http://127.0.0.1:{MOCK_PORT}/__log", headers=MOCK_AUTH).json()


def col_ids() -> dict:
    snap = httpx.get(f"{base}/api/snapshot").json()
    return {c["name"]: c["id"] for c in snap["collections"]}


def members(name: str) -> set:
    snap = httpx.get(f"{base}/api/snapshot").json()
    c = next(c for c in snap["collections"] if c["name"] == name)
    return {m["id"] for m in c["members"]}


def apply(cid: str, add=(), remove=(), dry_run=False, headers=HDR):
    return httpx.post(f"{base}/api/collections/{cid}/apply", headers=headers,
                      json={"add": list(add), "remove": list(remove), "dry_run": dry_run})


def writer_guard():
    print("writer guard (no network)")
    import asyncio
    from app.jellyfin import CollectionWriter
    w = CollectionWriter("http://127.0.0.1:1", "k")
    cid, item = "d" * 32, "b" * 32

    def refused(*args) -> bool:
        try:
            asyncio.run(w._send(*args))
        except ValueError:
            return True
        return False

    check("PUT refused", refused("PUT", cid, [item]))
    check("GET refused", refused("GET", cid, [item]))
    check("path-traversal collection id refused", refused("DELETE", "../Items", [item]))
    check("empty collection id (would hit /Items) refused", refused("DELETE", "", [item]))
    check("non-hex item id refused", refused("DELETE", cid, ["not-hex"]))

    calls = []

    class Resp:
        status_code, text = 204, ""

        def json(self):
            raise ValueError("no body")

    async def fake(method, path, params=None):
        calls.append((method, path, params))
        return Resp()

    w._client.request = fake
    asyncio.run(w.add(cid, [item]))
    asyncio.run(w.remove(cid, [item]))
    check("only collection-scoped POST and DELETE are ever built",
          calls == [("POST", f"/Collections/{cid}/Items", {"ids": item}),
                    ("DELETE", f"/Collections/{cid}/Items", {"ids": item})], str(calls))
    calls.clear()
    for bad in ("", "  x", "a\nb", "x" * 101):
        try:
            asyncio.run(w.create(bad)); ok = False
        except ValueError:
            ok = True
        check(f"create refuses bad name {bad[:8]!r}", ok)
    asyncio.run(w.create("Fine Name"))
    check("create sends only POST /Collections with name", calls == [("POST", "/Collections", {"name": "Fine Name"})], str(calls))
    asyncio.run(w.close())


def editor_flow():
    print("editor: dry run, apply, verify, skips, refusals")
    mock, app = scenario({})
    try:
        ids = col_ids()
        etv, star, empty = ids["ETV Discovery"], ids["Star Wars Collection"], ids["Empty Test"]

        r = apply(etv, add=[BLUEY], remove=[WILD_KRATTS], dry_run=True)
        j = r.json()
        check("dry run plans the change", r.status_code == 200 and j["added"] == [BLUEY] and j["removed"] == [WILD_KRATTS]
              and j["applied"] is False and j["dry_run"] is True, r.text)
        check("dry run sends nothing", mock_log() == [], str(mock_log()))

        r = apply(etv, add=[BLUEY], remove=[WILD_KRATTS])
        j = r.json()
        check("apply succeeds and verifies", r.status_code == 200 and j["applied"] and j["verified"] is True
              and j["mismatches"] == [], r.text)
        log = mock_log()
        check("exactly one POST then one DELETE, both collection-scoped",
              [(e["method"], e["path"], e["ids"]) for e in log] ==
              [("POST", f"/Collections/{etv}/Items", [BLUEY]), ("DELETE", f"/Collections/{etv}/Items", [WILD_KRATTS])], str(log))
        check("DELETE /Items was never called", not any(e.get("FORBIDDEN") for e in log))

        check("ETV membership updated", members("ETV Discovery") == {OCTONAUTS, REBOOT_94, BLUEY, MOVIE_SOME}, str(members("ETV Discovery")))
        snap = httpx.get(f"{base}/api/snapshot").json()
        unc = {x["id"] for x in snap["shows"] if x["id"] in set(snap["uncollected_ids"])}
        check("Wild Kratts now uncollected, Bluey no longer", WILD_KRATTS in unc and BLUEY not in unc, str(unc))
        wk = next(x for x in snap["shows"] if x["id"] == WILD_KRATTS)
        check("show itself still exists after removal", wk["name"] == "Wild Kratts")

        n = len(mock_log())
        r = apply(etv, add=[BLUEY], remove=[WILD_KRATTS])
        j = r.json()
        check("repeat is skipped, not resent", r.status_code == 200 and j["applied"] is False
              and j["skipped_add"] == [BLUEY] and j["skipped_remove"] == [WILD_KRATTS], r.text)
        check("no writes for a repeat", len(mock_log()) == n)

        n = len(mock_log())
        check("managed collection refused (409)", apply(star, add=[BLUEY]).status_code == 409)
        check("add and remove of same show refused", apply(etv, add=[DANIEL_TIGER], remove=[DANIEL_TIGER]).status_code == 400)
        check("unknown show refused", apply(etv, add=["f" * 32]).status_code == 400)
        check("non-hex id refused", apply(etv, add=["xyz"]).status_code == 400)
        check("unknown collection is 404", apply("e" * 32, add=[BLUEY]).status_code == 404)
        check("missing header refused", apply(etv, add=[DANIEL_TIGER], headers={}).status_code == 403)
        check("wrong header refused", apply(etv, add=[DANIEL_TIGER], headers={"X-Requested-With": "x"}).status_code == 403)
        check("refusals sent nothing to Jellyfin", len(mock_log()) == n, str(mock_log()[n:]))
        check("GET endpoints need no header", httpx.get(f"{base}/api/snapshot").status_code == 200)
    finally:
        stop(app, mock)


def editor_chunking():
    print("editor: writes are chunked")
    mock, app = scenario({}, {"JF_WRITE_CHUNK": "1"})
    try:
        empty = col_ids()["Empty Test"]
        r = apply(empty, add=[BLUEY, DANIEL_TIGER])
        posts = [e for e in mock_log() if e["method"] == "POST"]
        check("two shows sent as two requests", r.status_code == 200 and len(posts) == 2, str(mock_log()))
        check("both added", members("Empty Test") == {BLUEY, DANIEL_TIGER})
    finally:
        stop(app, mock)


def editor_partial_failure():
    print("editor: remove fails after add succeeded")
    mock, app = scenario({"MOCK_FAIL_REMOVE": "1"})
    try:
        etv = col_ids()["ETV Discovery"]
        r = apply(etv, add=[BLUEY], remove=[WILD_KRATTS])
        err = r.json().get("error", "")
        check("502 with a partial-apply message", r.status_code == 502 and "Partly applied" in err
              and "1 of 1 added" in err and "0 of 1 removed" in err, r.text)
        m = members("ETV Discovery")
        check("the add did happen and the app reports the real state", BLUEY in m and WILD_KRATTS in m, str(m))
    finally:
        stop(app, mock)


def editor_server_rejects_ids():
    print("editor: server rejects comma-separated ids")
    mock, app = scenario({"MOCK_REPEATED_ONLY": "1"})
    try:
        etv = col_ids()["ETV Discovery"]
        r = apply(etv, add=[BLUEY, DANIEL_TIGER])   # two ids, so the comma form is actually used
        err = r.json().get("error", "")
        check("surfaced as 502 with Jellyfin's status", r.status_code == 502 and "HTTP 400" in err and "0 of 2 added" in err, r.text)
        check("nothing changed", not ({BLUEY, DANIEL_TIGER} & members("ETV Discovery")))
    finally:
        stop(app, mock)


def movies_flow():
    print("movies: add and remove movies together with a show")
    mock, app = scenario({})
    try:
        evening = col_ids()["Evening Lineup"]
        r = apply(evening, add=[MOANA, ENCANTO, BLUEY])
        check("mixed add accepted and verified", r.status_code == 200 and r.json()["verified"] and len(r.json()["added"]) == 3, r.text)
        check("membership includes movies and show", {MOANA, ENCANTO, BLUEY} <= members("Evening Lineup"))
        r = apply(evening, remove=[MOANA])
        check("movie removed", r.status_code == 200 and MOANA not in members("Evening Lineup") and ENCANTO in members("Evening Lineup"), r.text)
        check("unknown movie id refused", apply(evening, add=["c99".ljust(32, "0")]).status_code == 400)
        log = mock_log()
        check("only collection-scoped writes, no DELETE /Items",
              all(e["path"] == f"/Collections/{evening}/Items" for e in log) and not any(e.get("FORBIDDEN") for e in log), str(log))
    finally:
        stop(app, mock)


def item_collections_flow():
    print("per-item collection editing")
    mock, app = scenario({})
    try:
        ids = col_ids()
        etv, evening, empty, sw = ids["ETV Discovery"], ids["Evening Lineup"], ids["Empty Test"], ids["Star Wars Collection"]
        url = f"{base}/api/items/{BLUEY}/collections"
        post = lambda body, item_url=url, headers=HDR: httpx.post(item_url, headers=headers, json=body)

        check("missing header refused (403)", post({"add_to": [etv]}, headers={}).status_code == 403)
        check("bad item id refused", post({"add_to": [etv]}, item_url=f"{base}/api/items/nothex/collections").status_code == 400)
        check("unknown item refused (404)", post({"add_to": [etv]}, item_url=f"{base}/api/items/{'f' * 32}/collections").status_code == 404)
        check("unknown collection refused (404)", post({"add_to": ["e" * 32]}).status_code == 404)
        check("managed collection refused, nothing written (409)",
              post({"add_to": [etv, sw]}).status_code == 409 and mock_log() == [], str(mock_log()))
        check("add and remove same collection refused", post({"add_to": [etv], "remove_from": [etv]}).status_code == 400)

        d = post({"add_to": [etv, evening], "dry_run": True}).json()
        check("dry run reports but writes nothing", d["added_to"] == ["ETV Discovery", "Evening Lineup"] and not d["applied"] and mock_log() == [], str(d))

        r = post({"add_to": [etv, empty]})
        j = r.json()
        check("added to two collections, verified", r.status_code == 200 and j["applied"] and j["verified"]
              and j["added_to"] == ["ETV Discovery", "Empty Test"], r.text)
        check("membership changed", BLUEY in members("ETV Discovery") and BLUEY in members("Empty Test"))
        log = mock_log()
        check("one single-id POST per collection",
              [(e["method"], e["path"], e["ids"]) for e in log] ==
              [("POST", f"/Collections/{etv}/Items", [BLUEY]), ("POST", f"/Collections/{empty}/Items", [BLUEY])], str(log))

        r = post({"add_to": [etv], "remove_from": [empty, evening]})
        j = r.json()
        check("already-in and not-in are skipped, not sent",
              r.status_code == 200 and j["added_to"] == [] and j["removed_from"] == ["Empty Test"]
              and sorted(j["skipped"]) == ["ETV Discovery", "Evening Lineup"], r.text)
        check("removed from one", BLUEY not in members("Empty Test") and BLUEY in members("ETV Discovery"))
        check("collection itself survives removal of its last member", "Empty Test" in col_ids())
        check("no forbidden call", not any(e.get("FORBIDDEN") for e in mock_log()))
        movie = httpx.post(f"{base}/api/items/{MOANA}/collections", headers=HDR, json={"add_to": [evening]})
        check("works for a movie too", movie.status_code == 200 and MOANA in members("Evening Lineup"), movie.text)
    finally:
        stop(app, mock)

    print("per-item collection editing: failure partway")
    mock, app = scenario({"MOCK_FAIL_REMOVE": "1"})
    try:
        ids = col_ids()
        etv, evening = ids["ETV Discovery"], ids["Evening Lineup"]
        r = httpx.post(f"{base}/api/items/{OCTONAUTS}/collections", headers=HDR,
                       json={"add_to": [ids["Empty Test"]], "remove_from": [etv, evening]})
        err = r.json().get("error", "")
        check("502 naming what already went through", r.status_code == 502 and 'added to "Empty Test"' in err and "Partly applied" in err, r.text)
        check("the add did happen", OCTONAUTS in members("Empty Test"))
    finally:
        stop(app, mock)

    print("per-item collection editing: read-only")
    mock, app = scenario({}, {"READ_ONLY": "1"})
    try:
        etv = col_ids()["ETV Discovery"]
        check("refused (403), nothing sent",
              httpx.post(f"{base}/api/items/{BLUEY}/collections", headers=HDR, json={"add_to": [etv]}).status_code == 403
              and mock_log() == [])
    finally:
        stop(app, mock)


def create_flow():
    print("create collection")
    mock, app = scenario({})
    try:
        url = f"{base}/api/collections"
        post = lambda name, **kw: httpx.post(url, headers=kw.get("headers", HDR), json={"name": name, "dry_run": kw.get("dry_run", False)})
        check("missing header refused (403)", post("Nope", headers={}).status_code == 403)
        check("blank name refused", post("   ").status_code == 400)
        check("control characters refused", post("bad\nname").status_code == 400)
        check("over-long name refused", post("x" * 101).status_code == 400)
        check("duplicate refused, case-insensitive (409)", post("etv discovery").status_code == 409)
        check("Auto Collections name refused (409)", post("middle earth").status_code == 409)
        d = post("Dry Run Name", dry_run=True)
        check("dry run creates nothing", d.status_code == 200 and d.json()["created"] is False and mock_log() == [], str(mock_log()))

        r = post("  Saturday Morning  ")
        j = r.json()
        check("created, trimmed, verified", r.status_code == 200 and j["created"] and j["verified"] and j["name"] == "Saturday Morning", r.text)
        log = mock_log()
        check("exactly one request: POST /Collections with only name",
              log == [{"method": "POST", "path": "/Collections", "name": "Saturday Morning", "params": ["name"]}], str(log))
        check("id matches the one in the snapshot", col_ids().get("Saturday Morning") == j["id"])
        check("new collection starts empty", members("Saturday Morning") == set())
        check("no forbidden call", not any(e.get("FORBIDDEN") for e in log))
        check("duplicate of the new one refused", post("Saturday Morning").status_code == 409)
        a = apply(j["id"], add=[BLUEY])
        check("can add a show to the new collection", a.status_code == 200 and members("Saturday Morning") == {BLUEY}, a.text)
    finally:
        stop(app, mock)

    print("create collection: server refuses an empty collection")
    mock, app = scenario({"MOCK_REFUSE_EMPTY_CREATE": "1"})
    try:
        r = httpx.post(f"{base}/api/collections", headers=HDR, json={"name": "Whatever"})
        check("surfaced as 502 with Jellyfin's status", r.status_code == 502 and "HTTP 400" in r.json().get("error", ""), r.text)
        check("nothing created", "Whatever" not in col_ids())
    finally:
        stop(app, mock)

    print("create collection: Jellyfin returns no id")
    mock, app = scenario({"MOCK_CREATE_NO_BODY": "1"})
    try:
        r = httpx.post(f"{base}/api/collections", headers=HDR, json={"name": "No Body"})
        check("found by name instead", r.status_code == 200 and r.json()["verified"] and r.json()["id"] == col_ids().get("No Body"), r.text)
    finally:
        stop(app, mock)

    print("create collection: read-only")
    mock, app = scenario({}, {"READ_ONLY": "1"})
    try:
        check("refused (403), nothing sent", httpx.post(f"{base}/api/collections", headers=HDR, json={"name": "X"}).status_code == 403
              and mock_log() == [])
    finally:
        stop(app, mock)


def editor_read_only():
    print("editor: read-only mode")
    mock, app = scenario({}, {"READ_ONLY": "1"})
    try:
        snap = httpx.get(f"{base}/api/snapshot").json()
        check("snapshot reports read_only", snap["read_only"] is True)
        etv = col_ids()["ETV Discovery"]
        check("apply refused (403)", apply(etv, add=[BLUEY]).status_code == 403)
        check("dry run also refused", apply(etv, add=[BLUEY], dry_run=True).status_code == 403)
        check("nothing sent", mock_log() == [], str(mock_log()))
    finally:
        stop(app, mock)


if __name__ == "__main__":
    config_tests()
    happy_path()
    native_disagrees()
    native_missing()
    bad_key()
    startup_without_config()
    writer_guard()
    editor_flow()
    editor_chunking()
    editor_partial_failure()
    editor_server_rejects_ids()
    editor_read_only()
    movies_flow()
    item_collections_flow()
    create_flow()
    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)
