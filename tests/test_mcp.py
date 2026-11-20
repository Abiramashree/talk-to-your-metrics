import sys

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from ttym.mcp_server import build_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def server(layer):
    return build_server(layer)


async def test_tools_and_schema(server):
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == {"list_metrics", "get_dimensions", "query_metrics"}
        props = tools["query_metrics"].input_schema["properties"]
        assert {"metrics", "group_by", "filters", "start", "end", "order_by", "limit"} <= set(props)


async def test_query_returns_rows_sql_and_definitions(server, answers):
    async with Client(server) as client:
        result = await client.call_tool("query_metrics", {"metrics": ["revenue"], "start": "2018-01-01", "end": "2018-03-31"})
        assert not result.is_error
        out = result.structured_content
        assert out["rows"][0][0] == pytest.approx(answers["d01"]["rows"][0][0])
        assert out["sql"].startswith("SELECT") and "revenue" in out["definitions"]


async def test_filters_pass_through(server, answers):
    async with Client(server) as client:
        result = await client.call_tool("query_metrics", {
            "metrics": ["payments"], "start": "2018-01-01", "end": "2018-12-31",
            "filters": [{"dimension": "payment__payment_type", "operator": "=", "value": "voucher"}],
        })
        assert result.structured_content["rows"][0][0] == pytest.approx(answers["d06"]["rows"][0][0])


async def test_errors_come_back_as_tool_errors(server):
    async with Client(server) as client:
        result = await client.call_tool("query_metrics", {"metrics": ["profit"]})
        assert result.is_error and "Unknown metric" in result.content[0].text
        result = await client.call_tool("query_metrics", {
            "metrics": ["revenue"], "filters": [{"dimension": "order__state", "operator": "like", "value": "%"}],
        })
        assert result.is_error


async def test_bare_server(layer):
    async with Client(build_server(layer.view(bare=True))) as client:
        metrics = (await client.call_tool("list_metrics", {})).structured_content["metrics"]
        assert all(set(m) == {"name"} for m in metrics)


async def test_stdio_subprocess():
    params = StdioServerParameters(command=sys.executable, args=["-m", "ttym.mcp_server"])
    async with Client(params) as client:
        out = (await client.call_tool("list_metrics", {})).structured_content
        assert len(out["metrics"]) == 18
