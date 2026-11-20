"""The web app: the page, the agent, REST and MCP over HTTP.

    uvicorn ttym.api:app       # page at /, REST at /metrics /dimensions /query, MCP at /mcp
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import FastAPI, Query as Param, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ttym.agent import ask
from ttym.core import Filter, Query, QueryError, SemanticLayer
from ttym.llm import LLM, LLMError, load_env
from ttym.mcp_server import FilterIn, Name, build_server

ROOT = pathlib.Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
load_env(ROOT / ".env")

PROVIDER = os.environ.get("TTYM_PROVIDER", "groq")
ASK_PER_IP = int(os.environ.get("TTYM_ASK_PER_IP", "10"))  # per 10 minutes
ASK_PER_DAY = int(os.environ.get("TTYM_ASK_PER_DAY", "300"))  # whole site
ASK_TIMEOUT = 90

layer = SemanticLayer(bare=os.environ.get("TTYM_BARE") == "1")
mcp = build_server(layer)
mcp_app = mcp.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="talk-to-your-metrics", lifespan=lifespan, docs_url="/docs", redoc_url=None)
app.router.routes.extend(mcp_app.routes)  # POST /mcp
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")


MAX_BODY = 64 * 1024


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method == "POST":
        length = request.headers.get("content-length")
        if length is None or not length.isdigit():
            return JSONResponse(status_code=411, content={"error": "A Content-Length header is required."})
        if int(length) > MAX_BODY:
            return JSONResponse(status_code=413, content={"error": "Request body too large."})
    response = await call_next(request)
    if not request.url.path.startswith("/docs"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
        )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path in ("/ask", "/query"):
        response.headers["Cache-Control"] = "no-store"
    return response


class Limiter:
    """In-memory limits: per client per 10 minutes, and for the whole site per day."""

    def __init__(self, per_ip: int, per_day: int) -> None:
        self.per_ip, self.per_day = per_ip, per_day
        self.recent: dict[str, deque] = defaultdict(deque)
        self.day, self.today_count = time.strftime("%Y-%m-%d"), 0

    def allow(self, client: str) -> str | None:
        now = time.time()
        if time.strftime("%Y-%m-%d") != self.day:
            self.day, self.today_count = time.strftime("%Y-%m-%d"), 0
        if self.today_count >= self.per_day:
            return "The daily question limit for this demo has been reached. Try again tomorrow."
        hits = self.recent[client]
        while hits and now - hits[0] > 600:
            hits.popleft()
        if len(hits) >= self.per_ip:
            return "Too many questions in a short time. Try again in a few minutes."
        hits.append(now)
        self.today_count += 1
        return None


limiter = Limiter(ASK_PER_IP, ASK_PER_DAY)
query_limiter = Limiter(per_ip=int(os.environ.get("TTYM_QUERY_PER_IP", "300")), per_day=int(os.environ.get("TTYM_QUERY_PER_DAY", "50000")))


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class QueryIn(BaseModel):
    metrics: list[Name] = Field(min_length=1, max_length=10)
    group_by: list[Name] = Field(default_factory=list, max_length=5)
    filters: list[FilterIn] = Field(default_factory=list, max_length=10)
    start: Name | None = None
    end: Name | None = None
    order_by: list[Name] = Field(default_factory=list, max_length=5)
    limit: int | None = None


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=300)


@app.exception_handler(QueryError)
async def query_error(_, exc: QueryError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.get("/", include_in_schema=False)
def page() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.get("/metrics")
def metrics() -> dict[str, Any]:
    return layer.list_metrics()


@app.get("/dimensions")
def dimensions(metrics: list[Name] = Param(min_length=1, max_length=10)) -> dict[str, Any]:
    return layer.get_dimensions(metrics)


@app.post("/query")
def query(body: QueryIn, request: Request) -> Any:
    refused = query_limiter.allow(_client(request))
    if refused:
        return JSONResponse(status_code=429, content={"error": refused})
    return layer.query(
        Query(
            metrics=body.metrics,
            group_by=body.group_by,
            filters=[Filter(f.dimension, f.operator, f.value) for f in body.filters],
            start=body.start,
            end=body.end,
            order_by=body.order_by,
            limit=body.limit,
        )
    )


@app.post("/ask")
async def ask_question(body: AskIn, request: Request) -> JSONResponse:
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        return JSONResponse(status_code=403, content={"error": "Cross-site requests are not allowed."})
    refused = limiter.allow(_client(request))
    if refused:
        return JSONResponse(status_code=429, content={"error": refused})
    try:
        llm = LLM(PROVIDER)
        out = await asyncio.wait_for(ask(body.question.strip(), mcp, llm), timeout=ASK_TIMEOUT)
    except LLMError:
        return JSONResponse(status_code=503, content={"error": "The assistant is unavailable right now."})
    except TimeoutError:
        return JSONResponse(status_code=504, content={"error": "That took too long. Try a simpler question."})
    if out.get("final") is None:
        return JSONResponse(status_code=502, content={"error": "The assistant did not produce an answer."})
    return JSONResponse({"final": out["final"], "working": working(out["trace"])})


def working(trace: list[dict]) -> dict | None:
    """What the page shows for an answer: the last successful metric query, editable, plus the agent's steps."""
    steps = [{"tool": s["tool"], "arguments": s["arguments"], "ok": not (isinstance(s["result"], dict) and "error" in s["result"])}
             for s in trace]
    for step in reversed(trace):
        result = step["result"]
        if step["tool"] == "query_metrics" and isinstance(result, dict) and "sql" in result:
            args = step["arguments"]
            return {
                "query": {
                    "metrics": args.get("metrics") or [],
                    "group_by": args.get("group_by") or [],
                    "filters": args.get("filters") or [],
                    "start": args.get("start"),
                    "end": args.get("end"),
                    "order_by": args.get("order_by") or [],
                    "limit": args.get("limit"),
                },
                "definitions": result.get("definitions", {}),
                "columns": result["columns"],
                "rows": result["rows"],
                "truncated": result.get("truncated", False),
                "sql": result["sql"],
                "steps": steps,
            }
    return {"query": None, "steps": steps} if steps else None
