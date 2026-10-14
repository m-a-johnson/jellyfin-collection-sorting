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
        check("ETV has 3 series and 1 other item", len(etv["members"]) == 3 and etv["other_count"] == 1, str(etv))
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
        check("native cross-check ran on 9 shows", any("9 sampled shows agree" in c["detail"] for c in sc), str(sc))
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


if __name__ == "__main__":
    config_tests()
    happy_path()
    native_disagrees()
    native_missing()
    bad_key()
    startup_without_config()
    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)
