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


def test_bad_values_are_400_not_500(client):
    for value in ["banana", float("nan")]:
        body = '{"metrics": ["revenue"], "filters": [{"dimension": "order__is_late", "operator": "=", "value": %s}]}'
        r = client.post("/query", content=body % ('"banana"' if value == "banana" else "NaN"), headers={"content-type": "application/json"})
        assert r.status_code == 400, r.text


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


def test_page_and_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>Talk to your metrics</title>" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/chat").text == r.text  # the chat view is the same page, opened at /chat


def test_ask_returns_answer_and_working(client, monkeypatch):
    from fakes import FakeLLM, call, final
    import ttym.api as api

    script = [
        call("query_metrics", {"metrics": ["revenue"], "filters": [{"dimension": "order__state", "operator": "=", "value": "SP"}]}),
        final({"answer": "x", "columns": ["revenue"], "rows": [[1.0]]}),
    ]
    monkeypatch.setattr(api, "LLM", lambda: FakeLLM(list(script)))
    monkeypatch.setattr(api, "limiter", api.Limiter(per_ip=10, per_day=10))
    r = client.post("/ask", json={"question": "Revenue in SP?"})
    assert r.status_code == 200
    working = r.json()["working"]
    assert working["query"]["filters"][0]["value"] == "SP" and "Excludes freight" in working["definitions"]["revenue"]
    assert working["query"]["metrics"] == ["revenue"] and working["steps"][0]["tool"] == "query_metrics"
    assert working["sql"].startswith("SELECT")


def test_ask_limits_and_validation(client, monkeypatch):
    from fakes import FakeLLM, final
    import ttym.api as api

    monkeypatch.setattr(api, "LLM", lambda: FakeLLM([final({"cannot_answer": "no"})] * 5))
    monkeypatch.setattr(api, "limiter", api.Limiter(per_ip=2, per_day=100))
    assert client.post("/ask", json={"question": "x" * 301}).status_code == 422
    codes = [client.post("/ask", json={"question": "Profit?"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_security_hardening(client, monkeypatch):
    from fakes import FakeLLM, final
    import ttym.api as api

    big = '{"metrics": ["' + "a" * 100_000 + '"]}'
    assert client.post("/query", content=big, headers={"content-type": "application/json"}).status_code == 413
    long_name = client.post("/query", json={"metrics": ["a" * 101]})
    assert long_name.status_code == 422 and len(long_name.text) < 5_000
    r = client.post("/query", json={"metrics": ["nope"]})
    assert r.status_code == 400 and r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY" and "max-age" in r.headers["strict-transport-security"]

    monkeypatch.setattr(api, "LLM", lambda: FakeLLM([final({"cannot_answer": "no"})] * 3))
    monkeypatch.setattr(api, "limiter", api.Limiter(per_ip=10, per_day=10))
    assert client.post("/ask", json={"question": "Profit?"}, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/ask", json={"question": "Profit?"}, headers={"origin": "http://testserver"}).status_code == 200


def test_page_table_uses_query_result_not_model_copy(client, monkeypatch):
    from fakes import FakeLLM, call, final
    import ttym.api as api

    script = [call("query_metrics", {"metrics": ["repeat_customers"]}),
              final({"answer": "x", "columns": ["repeat_customers"], "rows": [[999999]]})]  # model misreports
    monkeypatch.setattr(api, "LLM", lambda: FakeLLM(list(script)))
    monkeypatch.setattr(api, "limiter", api.Limiter(per_ip=10, per_day=10))
    data = client.post("/ask", json={"question": "Repeat customers?"}).json()
    assert data["working"]["rows"] == [[2997]]


def test_long_input_is_not_echoed(layer):
    from ttym.core import Query, QueryError

    with pytest.raises(QueryError) as e:
        layer.query(Query(["z" * 5000]))
    assert len(str(e.value)) < 1000


def test_playground_query_limit(client, monkeypatch):
    import ttym.api as api

    monkeypatch.setattr(api, "query_limiter", api.Limiter(per_ip=2, per_day=100))
    codes = [client.post("/query", json={"metrics": ["orders"]}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


@pytest.mark.parametrize("error, status, text", [
    ("quota", 503, "free LLM limit is used up"),
    ("network", 503, "can't be reached"),
])
def test_ask_failures_are_clear(client, monkeypatch, error, status, text):
    import ttym.api as api
    from ttym.llm import LLMError, QuotaExhausted

    class Failing:
        usage = {}

        async def chat(self, *a, **k):
            raise QuotaExhausted("x") if error == "quota" else LLMError("could not reach the model: ConnectError")

    monkeypatch.setattr(api, "LLM", lambda: Failing())
    monkeypatch.setattr(api, "limiter", api.Limiter(per_ip=10, per_day=10))
    r = client.post("/ask", json={"question": "Revenue?"})
    assert r.status_code == status and text in r.json()["error"]


def test_daily_cap_message(client, monkeypatch):
    import ttym.api as api

    monkeypatch.setattr(api, "limiter", api.Limiter(per_ip=10, per_day=0))
    r = client.post("/ask", json={"question": "Revenue?"})
    assert r.status_code == 429 and "for today" in r.json()["error"]
