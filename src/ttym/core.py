"""The semantic layer as three operations: list metrics, get dimensions, query.

This is the only module that imports MetricFlow. MCP and REST are thin wrappers around it.
Queries are structured (names, operators, values), never SQL, so callers cannot inject SQL.
"""

from __future__ import annotations

import copy
import datetime as dt
import decimal
import logging
import math
import os
import pathlib
import re
import threading
from dataclasses import dataclass, field
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
DBT_DIR = ROOT / "dbt"

GRAINS = ("day", "week", "month", "quarter", "year")
OPERATORS = ("=", "!=", ">", ">=", "<", "<=", "in", "not in")
MAX_ROWS = 1000
TODAY = os.environ.get("TTYM_TODAY", "2018-10-17")

# MetricFlow exposes every time column under several names; agents only need metric_time.
_TIME_DIMENSION = re.compile(r"(^|__)(purchased_at|first_order_at)$")


class QueryError(ValueError):
    """A request the semantic layer cannot answer. The message says why and what to do instead."""


@dataclass
class Filter:
    dimension: str
    operator: str
    value: Any


@dataclass
class Query:
    metrics: list[str]
    group_by: list[str] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    start: str | None = None
    end: str | None = None
    order_by: list[str] = field(default_factory=list)
    limit: int | None = None


class SemanticLayer:
    """Loads the dbt semantic manifest once and answers queries against DuckDB, read-only.

    bare=True removes descriptions, synonyms and example values from everything returned:
    benchmark setup B. The metrics, names and SQL are identical in both modes.
    """

    def __init__(self, bare: bool = False) -> None:
        os.environ.setdefault("DBT_TARGET", "serve")
        os.environ.setdefault("TTYM_DUCKDB", str(ROOT / "data" / "olist.duckdb"))
        logging.getLogger("metricflow").setLevel(logging.ERROR)
        from dbt_metricflow.cli.cli_configuration import CLIConfiguration  # internal; versions pinned

        config = CLIConfiguration()
        config.setup(dbt_profiles_path=DBT_DIR, dbt_project_path=DBT_DIR, configure_file_logging=False)
        self._mf = config.mf
        self._lock = threading.Lock()
        self.bare = bare
        self._metrics = {m.name: m for m in self._mf.list_metrics()}
        self._values: dict[str, list[str]] = {}
        # Which metrics each categorical dimension works with, so a mismatch can be explained.
        self._dimension_metrics: dict[str, list[str]] = {}
        for name in sorted(self._metrics):
            for d in self._categorical(name):
                self._dimension_metrics.setdefault(d, []).append(name)

    def view(self, bare: bool) -> SemanticLayer:
        """The same engine with metadata on or off. dbt keeps global state, so load it once per process."""
        other = copy.copy(self)
        other.bare = bare
        return other

    # --- discovery -------------------------------------------------------

    def list_metrics(self) -> dict:
        metrics = []
        for m in sorted(self._metrics.values(), key=lambda m: m.name):
            item: dict[str, Any] = {"name": m.name}
            if not self.bare:
                item["label"] = m.label
                item["description"] = m.description
                synonyms = (m.config.meta or {}).get("synonyms") if m.config else None
                if synonyms:
                    item["synonyms"] = synonyms
            metrics.append(item)
        return {"today": TODAY, "metrics": metrics}

    def get_dimensions(self, metrics: list[str]) -> dict:
        self._check_metrics(metrics)
        dims = []
        for name, d in sorted(self._shared_dimensions(metrics).items()):
            item: dict[str, Any] = {"name": name}
            if not self.bare:
                item["description"] = d.description
                item["values"] = self._dimension_values(metrics, name)
            dims.append(item)
        time = {"name": "metric_time", "grains": list(GRAINS)}
        if not self.bare:
            time["description"] = "When the order was placed. Group by metric_time__<grain>; filter with start/end."
        shared = self._shared_dimensions(metrics)
        unavailable = [self._unavailable(metrics, d) for d in sorted(self._dimension_metrics) if d not in shared]
        return {"metrics": metrics, "time": time, "dimensions": dims, "not_available": unavailable}

    # --- query -----------------------------------------------------------

    def query(self, q: Query) -> dict:
        from metricflow.engine.metricflow_engine import MetricFlowQueryRequest

        self._check_metrics(q.metrics)
        all_time = [m for m in q.metrics if self._is_all_time(m)]
        if all_time and (q.start or q.end or any(g.startswith("metric_time") for g in q.group_by)):
            raise QueryError(
                f"{all_time} count people over all time (two or more orders ever), so a date range or time "
                f"grouping would change their meaning. Query them without start, end or metric_time."
            )
        dims = self._shared_dimensions(q.metrics)
        group_by = [self._check_group_by(g, dims, q.metrics, allow_time=not all_time) for g in q.group_by]
        where = [self._render_filter(f, dims, q.metrics) for f in q.filters]
        start, end = self._parse_date(q.start, "start"), self._parse_date(q.end, "end")
        if start and end and start > end:
            raise QueryError(f"start {q.start} is after end {q.end}.")
        allowed_order = set(q.metrics) | set(group_by)
        order_by = q.order_by or group_by  # stable output: unordered results come back in arbitrary order
        for o in order_by:
            if o.removeprefix("-") not in allowed_order:
                raise QueryError(f"order_by '{_short(o)}' must be one of the requested metrics or group_by: {sorted(allowed_order)}.")
        if q.limit is not None and not 1 <= q.limit <= MAX_ROWS:
            raise QueryError(f"limit must be between 1 and {MAX_ROWS}.")
        limit = q.limit or MAX_ROWS

        request = MetricFlowQueryRequest.create(
            metric_names=q.metrics,
            group_by_names=group_by or None,
            where_constraints=where or None,
            time_constraint_start=start,
            time_constraint_end=end,
            order_by_names=order_by or None,
            limit=q.limit,
        )
        try:
            with self._lock:
                result = self._mf.query(request)
        except Exception as e:  # database or planner rejection: return a reason the caller can act on
            raise QueryError(f"The query was rejected: {_reason(e)}") from None
        table = result.result_df
        rows = [[_plain(v) for v in row] for row in table.rows]
        out: dict[str, Any] = {
            "columns": list(table.column_names),
            "rows": rows[:limit],
            "truncated": len(rows) > limit,
            "sql": _strip_comments(result.sql),
        }
        if not self.bare:
            out["definitions"] = {m: self._metrics[m].description for m in q.metrics}
        return out

    # --- validation ------------------------------------------------------

    def _check_metrics(self, metrics: list[str]) -> None:
        if not metrics:
            raise QueryError("Ask for at least one metric. Call list_metrics to see them.")
        unknown = [m for m in metrics if m not in self._metrics]
        if unknown:
            shown = [_short(m) for m in unknown[:5]]
            raise QueryError(
                f"Unknown metric(s): {shown}. This semantic layer has no such metric, so the question may "
                f"not be answerable from this data. Available: {sorted(self._metrics)}."
            )

    def _is_all_time(self, metric: str) -> bool:
        m = self._metrics[metric]
        return bool(m.config and (m.config.meta or {}).get("all_time"))

    def _categorical(self, metric: str) -> dict:
        return {d.dunder_name: d for d in self._metrics[metric].dimensions
                if not d.dunder_name.startswith("metric_time") and not _TIME_DIMENSION.search(d.dunder_name)}

    def _unavailable(self, metrics: list[str], dimension: str) -> dict:
        """Why these metrics can't be split by a dimension, and which metrics can."""
        item: dict[str, Any] = {"name": dimension, "works_with": self._dimension_metrics.get(dimension, [])}
        if not self.bare:
            if any(self._is_all_time(m) for m in metrics):
                why = "these metrics count customers over all time, not orders, so order attributes don't apply"
            else:
                why = LEVEL_REASONS.get(dimension.split("__", 1)[0], "it is recorded at a different level of detail than these metrics")
            item["why"] = why
        return item

    def _mismatch(self, verb: str, name: str, metrics: list[str], dims: dict) -> QueryError:
        if name in self._dimension_metrics:
            u = self._unavailable(metrics, name)
            why = f" because {u['why']}" if "why" in u else ""
            return QueryError(f"Cannot {verb} '{name}' with {metrics}{why}. It works with: {u['works_with']}.")
        return QueryError(f"Cannot {verb} '{_short(name)}' with these metrics. Available: {sorted(dims) or 'none'}.")

    def _shared_dimensions(self, metrics: list[str]) -> dict:
        """Categorical dimensions every requested metric can be grouped and filtered by."""
        per_metric = [self._categorical(m) for m in metrics]
        shared = set.intersection(*(set(p) for p in per_metric))
        return {name: per_metric[0][name] for name in shared}

    def _check_group_by(self, name: str, dims: dict, metrics: list[str], allow_time: bool = True) -> str:
        if name.startswith("metric_time"):
            grain = name.removeprefix("metric_time").removeprefix("__") or "day"
            if grain not in GRAINS:
                raise QueryError(f"Time grain '{_short(grain)}' is not supported. Use metric_time__<grain> with one of {list(GRAINS)}.")
            return f"metric_time__{grain}"
        if name not in dims:
            if name in self._dimension_metrics:
                raise self._mismatch("group by", name, metrics, dims)
            time = " and metric_time__<grain>" if allow_time else ""
            raise QueryError(f"Cannot group by '{_short(name)}' with these metrics. Available: {sorted(dims) or 'none'}{time}.")
        return name

    def _render_filter(self, f: Filter, dims: dict, metrics: list[str]) -> str:
        if f.dimension.startswith("metric_time"):
            raise QueryError("Filter time with start and end (YYYY-MM-DD), not with a filter.")
        if f.dimension not in dims:
            raise self._mismatch("filter by", f.dimension, metrics, dims)
        op = f.operator.lower().strip()
        if op not in OPERATORS:
            raise QueryError(f"Operator '{_short(f.operator)}' is not supported. Use one of {list(OPERATORS)}.")
        if op in ("in", "not in"):
            if not isinstance(f.value, list) or not f.value:
                raise QueryError(f"Operator '{op}' needs a non-empty list of values.")
            literal = "(" + ", ".join(_literal(v) for v in f.value) + ")"
        else:
            literal = _literal(f.value)
        return f"{{{{ Dimension('{f.dimension}') }}}} {op} {literal}"

    def _dimension_values(self, metrics: list[str], name: str, limit: int = 30) -> list[str]:
        key = f"{metrics[0]}|{name}"
        if key not in self._values:
            with self._lock:
                values = self._mf.get_dimension_values(metrics[:1], name)
                self._values[key] = [v for v in values if v != "None"]
        values = self._values[key]
        return values if len(values) <= limit else values[:limit] + [f"… {len(values) - limit} more"]

    @staticmethod
    def _parse_date(value: str | None, label: str) -> dt.datetime | None:
        if value is None:
            return None
        try:
            return dt.datetime.combine(dt.date.fromisoformat(value), dt.time())
        except (TypeError, ValueError):
            raise QueryError(f"{label} must be a date like 2017-01-01, got {_short(str(value))!r}.") from None


# Why a dimension doesn't apply to metrics at another level of detail, by the dimension's entity.
LEVEL_REASONS = {
    "product": "product category belongs to items, and one order can contain items from several categories",
    "payment": "payment type belongs to payments, and one order can be paid in several ways",
    "order": "it describes orders, and these metrics are not counted per order",
}


def _short(text: str, limit: int = 60) -> str:
    """Caller input repeated in an error message, cut short so errors can't be used to amplify requests."""
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def _literal(value: Any) -> str:
    """A SQL literal for a filter value. Rejects anything that is not a plain scalar."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise QueryError("Filter values must be finite numbers.")
        return repr(value)
    if isinstance(value, str):
        if "{" in value or "}" in value or len(value) > 200:
            raise QueryError("Filter values may not contain braces or exceed 200 characters.")
        return "'" + value.replace("'", "''") + "'"
    raise QueryError(f"Filter values must be text, numbers or true/false, got {type(value).__name__}.")


def _reason(error: Exception) -> str:
    """The database's own message (e.g. a failed type conversion), without stack or file details."""
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    lines = [line for line in lines if line not in ("Runtime Error", "Database Error")]
    message = lines[0] if lines else type(error).__name__
    return re.sub(r"(/[\w.-]+)+", "<path>", message)[:300]


def _plain(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time() else value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def _strip_comments(sql: str) -> str:
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    return "\n".join(lines).strip()
