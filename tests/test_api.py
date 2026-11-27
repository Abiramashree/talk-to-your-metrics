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
    assert r.status_code == 200 and len(r.json()["metrics"]) == 20


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
        assert len(out["metrics"]) == 20


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


def test_healthz_is_cheap_and_plain(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.text == "ok" and r.headers["x-content-type-options"] == "nosniff"


def test_client_ip_behind_proxy_uses_the_entry_cloud_run_appends(client, monkeypatch):
    from ttym import api
    monkeypatch.setattr(api, "query_limiter", api.Limiter(per_ip=2, per_day=100))
    body = {"metrics": ["orders"]}
    monkeypatch.setattr(api, "BEHIND_PROXY", True)
    # The client forges a different first entry each time; the last entry (added by Cloud Run) is the same.
    for forged in ("1.1.1.1", "2.2.2.2"):
        assert client.post("/query", json=body, headers={"x-forwarded-for": f"{forged}, 203.0.113.7"}).status_code == 200
    assert client.post("/query", json=body, headers={"x-forwarded-for": "3.3.3.3, 203.0.113.7"}).status_code == 429
    assert client.post("/query", json=body, headers={"x-forwarded-for": "203.0.113.8"}).status_code == 200


def test_forwarded_header_ignored_without_proxy_setting(client, monkeypatch):
    from ttym import api
    monkeypatch.setattr(api, "query_limiter", api.Limiter(per_ip=1, per_day=100))
    monkeypatch.setattr(api, "BEHIND_PROXY", False)
    body = {"metrics": ["orders"]}
    assert client.post("/query", json=body, headers={"x-forwarded-for": "1.1.1.1"}).status_code == 200
    assert client.post("/query", json=body, headers={"x-forwarded-for": "2.2.2.2"}).status_code == 429


def test_mcp_accepts_only_local_and_public_host():
    from ttym import api
    from mcp.server.transport_security import TransportSecurityMiddleware
    guard = TransportSecurityMiddleware(api.mcp_security("ttym-abc.a.run.app"))
    assert guard._validate_host("ttym-abc.a.run.app") and guard._validate_host("localhost:8000")
    assert not guard._validate_host("evil.example") and not guard._validate_host("ttym-abc.a.run.app.evil.example")
    assert guard._validate_origin("https://ttym-abc.a.run.app") and not guard._validate_origin("https://evil.example")
    local_only = TransportSecurityMiddleware(api.mcp_security(""))
    assert not local_only._validate_host("ttym-abc.a.run.app")


def test_mcp_rejects_a_foreign_host_header(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    headers={"host": "evil.example", "accept": "application/json, text/event-stream"})
    assert r.status_code in (400, 421)


def test_no_answer_is_logged_and_explained(client, monkeypatch):
    import logging

    from fakes import FakeLLM
    from ttym import api

    class Capture(logging.Handler):
        lines: list[str] = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    handler = Capture()
    api.log.addHandler(handler)
    monkeypatch.setattr(api, "LLM", lambda: FakeLLM([{"role": "assistant", "content": "not json"}] * 2))
    monkeypatch.setattr(api, "limiter", api.Limiter(per_ip=10, per_day=10))
    try:
        r = client.post("/ask", json={"question": "secret question text"})
    finally:
        api.log.removeHandler(handler)
    text = "\n".join(handler.lines)
    assert r.status_code == 502 and r.json()["error"] == api.NO_ANSWER_MESSAGE
    assert "no answer: no valid final answer" in text and "secret question text" not in text


def test_ask_passes_previous_turn_and_validates_it(client, monkeypatch):
    from fakes import FakeLLM, final
    from ttym import api
    llm = FakeLLM([final({"cannot_answer": "no"})])
    monkeypatch.setattr(api, "LLM", lambda: llm)
    monkeypatch.setattr(api, "limiter", api.Limiter(per_ip=10, per_day=10))
    previous = {"question": "Top 5 categories by revenue",
                "query": {"metrics": ["revenue"], "group_by": ["product__category"], "start": None, "limit": 5}}
    r = client.post("/ask", json={"question": "Which of these were canceled most?", "previous": previous})
    assert r.status_code == 200
    sent = llm.sent[0]["messages"][1]["content"]
    assert "Top 5 categories by revenue" in sent and '"limit": 5' in sent and '"start"' not in sent
    too_long = {"question": "x" * 301}
    assert client.post("/ask", json={"question": "Which of these?", "previous": too_long}).status_code == 422
    bad_query = {"question": "q", "query": {"metrics": []}}
    assert client.post("/ask", json={"question": "Which of these?", "previous": bad_query}).status_code == 422


def test_page_and_static_files_are_revalidated(client):
    for path in ("/", "/chat", "/static/app.js"):
        assert client.get(path).headers["cache-control"] == "no-cache"
