"""REST API and MCP over HTTP in one app.

    uvicorn ttym.api:app       # REST at /metrics, /dimensions, /query; MCP at /mcp
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

from fastapi import FastAPI, Query as Param
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ttym.core import Filter, Query, QueryError, SemanticLayer
from ttym.mcp_server import FilterIn, build_server

layer = SemanticLayer(bare=os.environ.get("TTYM_BARE") == "1")
mcp = build_server(layer)
mcp_app = mcp.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="talk-to-your-metrics", lifespan=lifespan, docs_url="/docs", redoc_url=None)
app.router.routes.extend(mcp_app.routes)  # POST /mcp


class QueryIn(BaseModel):
    metrics: list[str] = Field(min_length=1, max_length=10)
    group_by: list[str] = Field(default_factory=list, max_length=5)
    filters: list[FilterIn] = Field(default_factory=list, max_length=10)
    start: str | None = None
    end: str | None = None
    order_by: list[str] = Field(default_factory=list, max_length=5)
    limit: int | None = None


@app.exception_handler(QueryError)
async def query_error(_, exc: QueryError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.get("/metrics")
def metrics() -> dict[str, Any]:
    return layer.list_metrics()


@app.get("/dimensions")
def dimensions(metrics: list[str] = Param(min_length=1)) -> dict[str, Any]:
    return layer.get_dimensions(metrics)


@app.post("/query")
def query(body: QueryIn) -> dict[str, Any]:
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
