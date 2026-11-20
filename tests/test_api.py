import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client


@pytest.fixture(scope="module")
def client():
    from ttym.api import app

    with TestClient(app) as c:
        yield c


def test_metrics(client):
    r = client.get("/metrics")
    assert r.status_code == 200 and len(r.json()["metrics"]) == 18


def test_dimensions(client):
    r = client.get("/dimensions", params={"metrics": ["revenue", "orders"]})
    assert r.status_code == 200 and "order__status" in {d["name"] for d in r.json()["dimensions"]}


def test_query(client, answers):
    r = client.post("/query", json={"metrics": ["delivered_orders"], "start": "2018-03-01", "end": "2018-03-31"})
    assert r.status_code == 200 and r.json()["rows"][0][0] == answers["d02"]["rows"][0][0]


def test_query_errors_are_400_with_reason(client):
    r = client.post("/query", json={"metrics": ["profit"]})
    assert r.status_code == 400 and "Unknown metric" in r.json()["error"]


def test_query_validates_body(client):
    assert client.post("/query", json={"metrics": []}).status_code == 422
    bad_filter = {"metrics": ["revenue"], "filters": [{"dimension": "order__state", "operator": "like", "value": "%"}]}
    assert client.post("/query", json=bad_filter).status_code == 422


@pytest.fixture(scope="module")
def live_server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ttym.api:app", "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(f"{url}/metrics", timeout=1)
            break
        except httpx.TransportError:
            time.sleep(0.2)
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_over_http(live_server, anyio_backend):
    async with Client(f"{live_server}/mcp") as client:
        out = (await client.call_tool("list_metrics", {})).structured_content
        assert len(out["metrics"]) == 18
