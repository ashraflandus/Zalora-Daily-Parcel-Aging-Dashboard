"""Zalora Daily Parcel Aging Dashboard backend.

Serves the single-page dashboard at / and the API it needs under /api:
  - a small JSON document store (daily snapshots, SLA versions, update log, settings,
    action plan, NDD/SDD on-time pulls and remarks)
  - a store for uploaded raw daily files (gzip-compressed)
  - the signed-in viewer (Google SSO headers) and whether they may edit
  - a proxy to Metabase's MCP `query` tool, so the page's MBQL runs with the
    app's own Metabase API key instead of each viewer's connector

The page itself is the former claude.ai artifact, unchanged; static/claude-shim.js
gives it the same `window.claude.use(...)` interface on top of this API.
"""
import gzip
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import asyncmy
import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

STATIC = Path(__file__).parent / "static"
COLLECTIONS = {"snapshots", "rawChunks", "slaVersions", "log", "actions", "config", "otp", "otpr", "otpf"}
MAX_DOC_BYTES = 2_000_000
MAX_BLOB_BYTES = 20_000_000

METABASE_URL = os.getenv("METABASE_URL", "").rstrip("/")
METABASE_API_KEY = os.getenv("METABASE_API_KEY", "")
METABASE_MCP_PATH = os.getenv("METABASE_MCP_PATH", "/api/mcp")
EDITORS = {e.strip().lower() for e in os.getenv("EDITOR_EMAILS", "").split(",") if e.strip()}

_pool = None
_http: httpx.AsyncClient | None = None


def _dsn() -> dict:
    # DATABASE_URL looks like: mysql://user%40tenant:password@host:2881/dbname
    u = urlparse(os.environ["DATABASE_URL"])
    return {
        "host": u.hostname,
        "port": u.port or 2881,
        "user": unquote(u.username or ""),
        "password": unquote(u.password or ""),
        "db": (u.path or "/").lstrip("/"),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool, _http
    if os.getenv("DATABASE_URL"):
        _pool = await asyncmy.create_pool(**_dsn(), autocommit=True)
    _http = httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0))
    yield
    await _http.aclose()
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()


app = FastAPI(title="Zalora Daily Parcel Aging Dashboard", lifespan=lifespan)


# ---------------------------------------------------------------- identity

def _email(request: Request) -> str | None:
    e = request.headers.get("x-forwarded-email")
    return e.lower() if e else None


def _can_write(email: str | None) -> bool:
    return not EDITORS or (email is not None and email in EDITORS)


def _require_writer(request: Request) -> str | None:
    email = _email(request)
    if not _can_write(email):
        raise HTTPException(403, "Edit access needed")
    return email


class Health(BaseModel):
    status: str


class Me(BaseModel):
    email: str | None
    user: str | None
    can_write: bool


@app.get("/health", response_model=Health)
def health():
    return {"status": "ok"}


@app.get("/api/me", response_model=Me)
def me(request: Request):
    email = _email(request)
    return {"email": email, "user": request.headers.get("x-forwarded-user"), "can_write": _can_write(email)}


# ---------------------------------------------------------------- documents

class Doc(BaseModel):
    id: str
    data: dict[str, Any]


class DocPage(BaseModel):
    docs: list[Doc]
    ids: list[str]
    cursor: int


class Ok(BaseModel):
    ok: bool


def _check(collection: str, doc_id: str | None = None):
    if _pool is None:
        raise HTTPException(503, "Database not configured")
    if collection not in COLLECTIONS:
        raise HTTPException(404, "Unknown collection")
    if doc_id is not None and not (0 < len(doc_id) <= 191):
        raise HTTPException(400, "Bad document id")


@app.get("/api/db/{collection}", response_model=DocPage)
async def list_docs(collection: str, since: int = 0):
    """Documents changed after `since` (a cursor from an earlier call), plus every current id so deletions show."""
    _check(collection)
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT doc_id, updated_us FROM docs WHERE collection=%s", (collection,))
        idx = await cur.fetchall()
        await cur.execute(
            "SELECT doc_id, data FROM docs WHERE collection=%s AND updated_us>%s", (collection, since)
        )
        rows = await cur.fetchall()
    cursor = max([since] + [r[1] for r in idx])
    return {"docs": [{"id": r[0], "data": json.loads(r[1])} for r in rows], "ids": [r[0] for r in idx], "cursor": cursor}


@app.get("/api/db/{collection}/{doc_id}", response_model=Doc)
async def get_doc(collection: str, doc_id: str):
    """Read one document."""
    _check(collection, doc_id)
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT data FROM docs WHERE collection=%s AND doc_id=%s", (collection, doc_id))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(404, "No such document")
    return {"id": doc_id, "data": json.loads(row[0])}


@app.put("/api/db/{collection}/{doc_id}", response_model=Ok)
async def set_doc(collection: str, doc_id: str, request: Request, data: dict[str, Any] = Body(...)):
    """Replace one document."""
    _check(collection, doc_id)
    email = _require_writer(request)
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    if len(raw.encode()) > MAX_DOC_BYTES:
        raise HTTPException(413, "Document too large")
    now = time.time_ns() // 1000
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO docs (collection, doc_id, data, updated_us, updated_by) VALUES (%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE data=VALUES(data), updated_us=VALUES(updated_us), updated_by=VALUES(updated_by)",
            (collection, doc_id, raw, now, email),
        )
    return {"ok": True}


@app.delete("/api/db/{collection}/{doc_id}", response_model=Ok)
async def delete_doc(collection: str, doc_id: str, request: Request):
    """Delete one document."""
    _check(collection, doc_id)
    _require_writer(request)
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM docs WHERE collection=%s AND doc_id=%s", (collection, doc_id))
    return {"ok": True}


# ---------------------------------------------------------------- files (raw daily uploads)

class BlobRef(BaseModel):
    id: str
    size: int


@app.post("/api/blobs", response_model=BlobRef)
async def upload_blob(request: Request):
    """Store one file (the request body, gzip-compressed at rest) and return its id."""
    if _pool is None:
        raise HTTPException(503, "Database not configured")
    email = _require_writer(request)
    body = await request.body()
    if not body:
        raise HTTPException(400, "Empty file")
    if len(body) > MAX_BLOB_BYTES:
        raise HTTPException(413, "File too large")
    blob_id = uuid.uuid4().hex
    ctype = (request.headers.get("content-type") or "application/octet-stream")[:128]
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO blobs (id, content_type, data, size, created_us, created_by) VALUES (%s,%s,%s,%s,%s,%s)",
            (blob_id, ctype, gzip.compress(body), len(body), time.time_ns() // 1000, email),
        )
    return {"id": blob_id, "size": len(body)}


@app.get("/api/blobs/{blob_id}")
async def get_blob(blob_id: str):
    """Return one stored file."""
    if _pool is None:
        raise HTTPException(503, "Database not configured")
    if not (0 < len(blob_id) <= 32 and blob_id.isalnum()):
        raise HTTPException(400, "Bad file id")
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT content_type, data FROM blobs WHERE id=%s", (blob_id,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(404, "No such file")
    return Response(gzip.decompress(row[1]), media_type=row[0])


# ---------------------------------------------------------------- Metabase (MCP query tool)

class MbQuery(BaseModel):
    query: dict[str, Any] | None = None
    query_handle: str | None = None
    continuation_token: str | None = None


class MbResult(BaseModel):
    payload: Any


_mcp_session: str | None = None
_rpc_id = 0


def _mcp_headers() -> dict:
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
         "x-api-key": METABASE_API_KEY}
    if _mcp_session:
        h["Mcp-Session-Id"] = _mcp_session
    return h


def _rpc_body(resp: httpx.Response) -> dict:
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        msg = None
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                d = json.loads(line[5:].strip())
                if "result" in d or "error" in d:
                    msg = d
        return msg or {}
    return resp.json() if resp.content else {}


async def _refusal_reason(mcp_status: int) -> str:
    """Tell a bad key apart from an MCP endpoint that won't take API keys, by trying the key on the REST API."""
    try:
        r = await _http.get(METABASE_URL + "/api/user/current", headers={"x-api-key": METABASE_API_KEY})
        rest = r.status_code
    except httpx.HTTPError:
        rest = None
    if rest == 200:
        return (f"Metabase accepts the API key on its REST API but its MCP endpoint ({METABASE_MCP_PATH}) "
                f"refused it (HTTP {mcp_status}); the MCP endpoint may not allow API-key access")
    if rest in (401, 403):
        return f"Metabase refused the API key (HTTP {rest}); check METABASE_API_KEY"
    return f"Metabase's MCP endpoint refused the API key (HTTP {mcp_status})"


async def _rpc(method: str, params: dict | None = None, notify: bool = False) -> dict:
    global _rpc_id
    _rpc_id += 1
    body: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not notify:
        body["id"] = _rpc_id
    resp = await _http.post(METABASE_URL + METABASE_MCP_PATH, json=body, headers=_mcp_headers())
    if resp.status_code in (401, 403):
        raise HTTPException(502, await _refusal_reason(resp.status_code))
    if resp.status_code == 404 and _mcp_session:
        raise LookupError("session expired")
    resp.raise_for_status()
    return {"resp": resp, "body": _rpc_body(resp)}


async def _mcp_init():
    global _mcp_session
    _mcp_session = None
    r = await _rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "zalora-parcel-aging", "version": "1"}})
    _mcp_session = r["resp"].headers.get("mcp-session-id")
    await _rpc("notifications/initialized", notify=True)


def _unwrap(result: dict) -> Any:
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    for c in result.get("content") or []:
        if c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except ValueError:
                return {"status": "failed", "error": c["text"]}
    return result


@app.post("/api/metabase/query", response_model=MbResult)
async def metabase_query(q: MbQuery, request: Request):
    """Run one page of an MBQL query (or fetch the next page by continuation token) through Metabase's MCP `query` tool."""
    _require_writer(request)
    if not METABASE_URL or not METABASE_API_KEY:
        raise HTTPException(503, "Metabase is not configured (METABASE_URL / METABASE_API_KEY)")
    args = {"query": q.query, "query_handle": q.query_handle, "continuation_token": q.continuation_token}
    for attempt in range(2):
        try:
            if _mcp_session is None:
                await _mcp_init()
            r = await _rpc("tools/call", {"name": "query", "arguments": args})
            break
        except LookupError:
            await _mcp_init()
    else:
        raise HTTPException(502, "Metabase session could not be opened")
    body = r["body"]
    if "error" in body:
        return {"payload": {"status": "failed", "error": body["error"].get("message", "Metabase error")}}
    return {"payload": _unwrap(body.get("result") or {})}


# ---------------------------------------------------------------- page

_SHIM = '<script src="/claude-shim.js"></script>'


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return html.replace("<head>", "<head>" + _SHIM, 1)


@app.get("/import", response_class=HTMLResponse, include_in_schema=False)
def import_page():
    return (STATIC / "import.html").read_text(encoding="utf-8")


@app.get("/claude-shim.js", include_in_schema=False)
def shim():
    return Response((STATIC / "claude-shim.js").read_text(encoding="utf-8"), media_type="application/javascript")
