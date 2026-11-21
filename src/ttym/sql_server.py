"""Benchmark setup A: an MCP server that lets the model write its own SQL against the raw Olist tables.

The fair baseline: the model gets the full raw schema (types, row counts, and the values of
low-cardinality text columns) and a read-only DuckDB connection. No semantic layer, no definitions.
"""

from __future__ import annotations

import datetime as dt
import decimal
import pathlib
import threading
from typing import Annotated, Any

import duckdb
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import StringConstraints

from ttym.core import TODAY

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAX_ROWS = 200
TIMEOUT = 10  # seconds per query
LOW_CARDINALITY = 10

SYSTEM = f"""\
You answer questions about the Olist marketplace (orders 2016-09 to 2018-10) by writing DuckDB SQL. Today is {TODAY}.
Call get_schema first, then run_sql. Never compute or estimate a number yourself; copy numbers from query results.
If the data cannot answer the question, do not substitute a different measure: say it cannot be answered.

When you are done, reply with JSON only, no other text:
{{"answer": "<one short sentence>", "columns": [...], "rows": [[...]]}}  with columns and rows copied from the query result
{{"cannot_answer": "<one short sentence: why>"}}  when the data cannot answer the question"""


RAW_ONLY = ROOT / "data" / "raw_only.duckdb"


def ensure_raw_only(source: pathlib.Path = ROOT / "data" / "olist.duckdb", target: pathlib.Path = RAW_ONLY) -> pathlib.Path:
    """A copy holding only the raw tables, so setup A can't reach the semantic layer's cleaned tables."""
    if not target.exists():
        tmp = target.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        con = duckdb.connect(str(tmp))
        con.execute(f"attach '{source}' as src (read_only)")
        con.execute("create schema raw")
        for (table,) in con.execute(
                "select table_name from information_schema.tables where table_catalog = 'src' and table_schema = 'raw'").fetchall():
            con.execute(f'create table raw."{table}" as select * from src.raw."{table}"')
        con.execute("detach src")
        con.close()
        tmp.rename(target)
    return target


def connect(path: pathlib.Path | None = None) -> duckdb.DuckDBPyConnection:
    """Read-only, raw tables only, and DuckDB may not read files or URLs."""
    return duckdb.connect(str(path or ensure_raw_only()), read_only=True, config={"enable_external_access": False})


def schema(con: duckdb.DuckDBPyConnection) -> str:
    lines = []
    tables = con.execute(
        "select table_name from information_schema.tables where table_schema = 'raw' order by table_name").fetchall()
    for (table,) in tables:
        rows = con.execute(f'select count(*) from raw."{table}"').fetchone()[0]
        lines.append(f"raw.{table} ({rows:,} rows)")
        columns = con.execute(
            "select column_name, data_type from information_schema.columns "
            "where table_schema = 'raw' and table_name = ? order by ordinal_position", [table]).fetchall()
        for name, dtype in columns:
            note = ""
            if dtype == "VARCHAR":
                values = con.execute(
                    f'select distinct "{name}" from raw."{table}" where "{name}" is not null limit {LOW_CARDINALITY + 1}').fetchall()
                if len(values) <= LOW_CARDINALITY:
                    note = "  values: " + ", ".join(sorted(str(v[0]) for v in values))
            lines.append(f"  {name} {dtype}{note}")
    return "\n".join(lines)


def _plain(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def build_sql_server(con: duckdb.DuckDBPyConnection | None = None) -> MCPServer:
    con = con or connect()
    text = schema(con)
    lock = threading.Lock()
    server = MCPServer(name="olist-sql", instructions="Answer questions about Olist by writing DuckDB SQL.")

    @server.tool()
    def get_schema() -> dict[str, Any]:
        """The raw Olist tables: columns, types, row counts, and the values of low-cardinality text columns."""
        return {"today": TODAY, "schema": text}

    @server.tool()
    def run_sql(sql: Annotated[str, StringConstraints(max_length=8000)]) -> dict[str, Any]:
        """Run one read-only DuckDB SELECT query. Returns at most 200 rows."""
        with lock:
            timer = threading.Timer(TIMEOUT, con.interrupt)
            timer.start()
            try:
                result = con.execute(sql)
                columns = [d[0] for d in result.description or []]
                rows = result.fetchmany(MAX_ROWS + 1)
            except duckdb.Error as e:
                raise ToolError(f"SQL error: {str(e).splitlines()[0][:300]}") from None
            finally:
                timer.cancel()
        return {"columns": columns, "rows": [[_plain(v) for v in r] for r in rows[:MAX_ROWS]],
                "truncated": len(rows) > MAX_ROWS, "sql": sql}

    return server
