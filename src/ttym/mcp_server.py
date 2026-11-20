"""MCP server: the semantic layer as three tools.

    python -m ttym.mcp_server            # stdio, for Claude Desktop / Claude Code / Cursor
    python -m ttym.mcp_server --bare     # benchmark setup B: no descriptions or synonyms
"""

from __future__ import annotations

import argparse
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, StringConstraints

from ttym.core import MAX_ROWS, Filter, Query, QueryError, SemanticLayer

Name = Annotated[str, StringConstraints(max_length=100)]
Value = str | int | float | bool

INSTRUCTIONS = """\
Answers business questions about the Olist marketplace (orders 2016-09 to 2018-10) from governed metrics.
Workflow: list_metrics, then get_dimensions for the metrics you need, then query_metrics.
Never compute a number yourself when a metric exists. If no metric fits the question, say the data cannot answer it.
"""


class FilterIn(BaseModel):
    dimension: Name = Field(description="A dimension name from get_dimensions, e.g. order__state.")
    operator: Literal["=", "!=", ">", ">=", "<", "<=", "in", "not in"]
    value: Annotated[str, StringConstraints(max_length=200)] | int | float | bool | list[Value] = Field(
        description="A value, or a list of values for in / not in."
    )


def build_server(layer: SemanticLayer) -> MCPServer:
    server = MCPServer(name="talk-to-your-metrics", instructions=INSTRUCTIONS)

    @server.tool()
    def list_metrics() -> dict[str, Any]:
        """List every metric that can be queried, and today's date for relative questions."""
        return layer.list_metrics()

    @server.tool()
    def get_dimensions(metrics: list[Name]) -> dict[str, Any]:
        """List the dimensions the given metrics can all be grouped and filtered by, and the time grains."""
        try:
            return layer.get_dimensions(metrics)
        except QueryError as e:
            raise ToolError(str(e)) from None

    @server.tool()
    def query_metrics(
        metrics: list[Name],
        group_by: list[Name] | None = None,
        filters: list[FilterIn] | None = None,
        start: Name | None = None,
        end: Name | None = None,
        order_by: list[Name] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Query metrics. Returns rows and the SQL that produced them.

        group_by: dimension names, or metric_time__<grain> (day, week, month, quarter, year).
        filters: conditions on dimensions. Filter time with start / end instead (YYYY-MM-DD, inclusive).
        order_by: metric or group_by names; prefix with - for descending.
        limit: at most 1000 rows.
        """
        try:
            return layer.query(
                Query(
                    metrics=metrics,
                    group_by=group_by or [],
                    filters=[Filter(f.dimension, f.operator, f.value) for f in filters or []],
                    start=start,
                    end=end,
                    order_by=order_by or [],
                    limit=limit,
                )
            )
        except QueryError as e:
            raise ToolError(str(e)) from None

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bare", action="store_true", help="omit descriptions, synonyms and example values")
    args = parser.parse_args()
    build_server(SemanticLayer(bare=args.bare)).run("stdio")


if __name__ == "__main__":
    main()
