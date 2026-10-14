"""Minimal imitation of the Jellyfin endpoints the app reads. Test use only.

Scenario switches (environment variables):
  MOCK_BREAK_NATIVE=1  /Items/{id}/Collections returns nothing (disagrees with member lists)
  MOCK_NO_NATIVE=1     /Items/{id}/Collections returns 404
  MOCK_NO_AUTOCOL=1    /AutoCollections/ExportConfiguration returns 404
  MOCK_REPEATED_ONLY=1 collection writes reject comma-separated ids with HTTP 400
  MOCK_FAIL_REMOVE=1   DELETE /Collections/{id}/Items returns HTTP 500
  MOCK_REFUSE_EMPTY_CREATE=1  POST /Collections without ids returns HTTP 400
  MOCK_CREATE_NO_BODY=1       POST /Collections returns 204 with no id

Every write is recorded and readable at GET /__log, including any call to
DELETE /Items (which the app must never send).
"""
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI()
KEY = "testkey"


def dashed(h: str) -> str:
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def hid(prefix: str, n: int) -> str:
    return (prefix + f"{n:02d}").ljust(32, "0")


KIDS, ADULT, COLS, MOVIES = (hid("a", 1), hid("a", 2), hid("a", 3), hid("a", 4))
LIBS = [
    {"Name": "Kids Shows", "ItemId": KIDS, "CollectionType": "tvshows"},
    {"Name": "Adult Shows", "ItemId": ADULT, "CollectionType": "tvshows"},
    {"Name": "Collections", "ItemId": COLS, "CollectionType": "boxsets"},
    {"Name": "Movies", "ItemId": MOVIES, "CollectionType": "movies"},
]


def series(n, name, year, lib):
    return {"Id": hid("b", n), "Name": name, "ProductionYear": year, "Type": "Series", "lib": lib}


SERIES = [
    series(1, "Bluey", 2018, KIDS), series(2, "Octonauts", 2010, KIDS),
    series(3, "Wild Kratts", 2011, KIDS), series(4, "Reboot", 1994, KIDS),
    series(5, "Reboot", 2022, KIDS), series(6, "Star Wars Rebels", 2014, KIDS),
    series(7, "Daniel Tiger", 2012, KIDS), series(8, "Severance", 2022, ADULT),
    series(9, "Andor", 2022, ADULT),
]
def movie(n, name, year):
    return {"Id": hid("c", n), "Name": name, "ProductionYear": year, "Type": "Movie", "lib": MOVIES}


MOVIE = movie(1, "Some Movie", 2001)
MOVIE_LIST = [MOVIE, movie(2, "Moana", 2016), movie(3, "Encanto", 2021), movie(4, "Paddington", 2014)]
BY_ID = {s["Id"]: s for s in SERIES + MOVIE_LIST}


def box(n, name, members):
    return {"Id": hid("d", n), "Name": name, "Type": "BoxSet", "members": members}


BOXSETS = [
    box(1, "ETV Discovery", [BY_ID[hid("b", 2)], BY_ID[hid("b", 3)], BY_ID[hid("b", 4)], MOVIE]),
    box(2, "Star Wars Collection", [BY_ID[hid("b", 6)]]),
    box(3, "Empty Test", []),
    box(4, "Evening Lineup", [BY_ID[hid("b", 2)], BY_ID[hid("b", 8)]]),
]


def strip(item):
    return {k: v for k, v in item.items() if k not in ("lib", "members")}


def norm(v):
    return v.replace("-", "").lower()


@app.middleware("http")
async def auth(request: Request, call_next):
    if request.url.path != "/System/Info/Public":
        if f'Token="{KEY}"' not in request.headers.get("authorization", ""):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/System/Info/Public")
def info():
    return {"ServerName": "MockJelly", "Version": "12.1.0"}


@app.get("/Library/VirtualFolders")
def folders():
    return [dict(l, ItemId=dashed(l["ItemId"])) for l in LIBS]  # dashed on purpose


@app.get("/Items")
def items(request: Request):
    q = request.query_params
    types, parent = q.get("includeItemTypes"), norm(q.get("parentId", ""))
    start, limit = int(q.get("startIndex", 0)), int(q.get("limit", 100))
    box_match = next((b for b in BOXSETS if b["Id"] == parent), None)
    if box_match is not None:
        rows = box_match["members"]
    elif types == "Series":
        rows = [s for s in SERIES if not parent or s["lib"] == parent]
    elif types == "Movie":
        rows = [m for m in MOVIE_LIST if not parent or m["lib"] == parent]
    elif types == "BoxSet":
        rows = BOXSETS
    else:
        rows = []
    page = [strip(r) for r in rows[start:start + limit]]
    return {"Items": page, "TotalRecordCount": len(rows), "StartIndex": start}


@app.get("/Items/{item_id}/Collections")
def item_collections(item_id: str):
    if os.environ.get("MOCK_NO_NATIVE"):
        raise HTTPException(404, "not found")
    if os.environ.get("MOCK_BREAK_NATIVE"):
        return {"Items": [], "TotalRecordCount": 0}
    iid = norm(item_id)
    hits = [b for b in BOXSETS if any(m["Id"] == iid for m in b["members"])]
    return {"Items": [dict(strip(b), Id=dashed(b["Id"])) for b in hits], "TotalRecordCount": len(hits)}


@app.get("/AutoCollections/ExportConfiguration")
def export():
    if os.environ.get("MOCK_NO_AUTOCOL"):
        raise HTTPException(404, "not found")
    return {"TitleMatchPairs": [{"TitleMatch": "Star Wars", "CollectionName": "Star Wars Collection"},
                                {"TitleMatch": "Middle Earth", "CollectionName": "Middle Earth"}],
            "ExpressionCollections": []}


LOG: list[dict] = []


def _write(method: str, cid: str, request: Request):
    raw = request.query_params.getlist("ids")
    ids = [x for r in raw for x in r.split(",") if x]
    LOG.append({"method": method, "path": request.url.path, "ids": ids})
    if os.environ.get("MOCK_REPEATED_ONLY") and any("," in r for r in raw):
        return JSONResponse({"error": "ids must be repeated parameters"}, status_code=400)
    if method == "DELETE" and os.environ.get("MOCK_FAIL_REMOVE"):
        return JSONResponse({"error": "boom"}, status_code=500)
    target = next((b for b in BOXSETS if b["Id"] == norm(cid)), None)
    if target is None:
        raise HTTPException(404, "collection not found")
    for raw_id in ids:
        i = norm(raw_id)
        if method == "POST":
            item = BY_ID.get(i)
            if item and all(m["Id"] != i for m in target["members"]):
                target["members"].append(item)
        else:
            target["members"] = [m for m in target["members"] if m["Id"] != i]
    return Response(status_code=204)


@app.post("/Collections")
def create_collection(request: Request):
    name = request.query_params.get("name", "")
    LOG.append({"method": "POST", "path": request.url.path, "name": name,
                "params": sorted(request.query_params.keys())})
    if os.environ.get("MOCK_REFUSE_EMPTY_CREATE") and not request.query_params.get("ids"):
        return JSONResponse({"error": "ids required"}, status_code=400)
    new = box(len(BOXSETS) + 1, name, [])
    BOXSETS.append(new)
    if os.environ.get("MOCK_CREATE_NO_BODY"):
        return Response(status_code=204)
    return {"Id": dashed(new["Id"])}


@app.post("/Collections/{cid}/Items")
def add_items(cid: str, request: Request):
    return _write("POST", cid, request)


@app.delete("/Collections/{cid}/Items")
def remove_items(cid: str, request: Request):
    return _write("DELETE", cid, request)


@app.delete("/Items")
def forbidden_delete(request: Request):
    LOG.append({"method": "DELETE", "path": "/Items", "ids": [], "FORBIDDEN": True})
    return Response(status_code=204)


@app.get("/__log")
def log():
    return LOG
