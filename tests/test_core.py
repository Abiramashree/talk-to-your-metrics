import pytest

from ttym.core import Filter, Query, QueryError

# Dev questions only. The test split is never run through the layer outside the benchmark.
DEV = {
    "d01": Query(["revenue"], start="2018-01-01", end="2018-03-31"),
    "d02": Query(["delivered_orders"], start="2018-03-01", end="2018-03-31"),
    "d03": Query(["average_review_score"], start="2018-01-01", end="2018-12-31"),
    "d04": Query(["late_delivery_rate"]),
    "d05": Query(["gross_merchandise_value"], start="2018-01-01", end="2018-12-31"),
    "d06": Query(["payments"], filters=[Filter("payment__payment_type", "=", "voucher")], start="2018-01-01", end="2018-12-31"),
}


@pytest.mark.parametrize("qid", DEV)
def test_dev_answers_match(layer, answers, qid):
    got = layer.query(DEV[qid])["rows"][0][0]
    assert got == pytest.approx(answers[qid]["rows"][0][0], abs=1e-6)


def test_list_metrics(layer):
    out = layer.list_metrics()
    names = {m["name"] for m in out["metrics"]}
    assert len(names) == 18 and {"revenue", "repeat_customers", "late_delivery_rate"} <= names
    revenue = next(m for m in out["metrics"] if m["name"] == "revenue")
    assert "Excludes freight" in revenue["description"] and "sales" in revenue["synonyms"]
    assert out["today"] == "2018-10-17"


def test_bare_view_strips_metadata_only(layer):
    bare = layer.view(bare=True)
    assert all(set(m) == {"name"} for m in bare.list_metrics()["metrics"])
    assert all(set(d) == {"name"} for d in bare.get_dimensions(["revenue"])["dimensions"])
    q = Query(["revenue"], group_by=["metric_time__year"])
    full, stripped = layer.query(q), bare.query(q)
    assert full["rows"] == stripped["rows"] and full["sql"] == stripped["sql"]
    assert "definitions" in full and "definitions" not in stripped
    assert layer.bare is False


def test_dimensions_hide_duplicate_time_columns(layer):
    names = {d["name"] for d in layer.get_dimensions(["revenue"])["dimensions"]}
    assert names == {"order__is_late", "order__review_score", "order__state", "order__status", "product__category"}
    status = next(d for d in layer.get_dimensions(["orders"])["dimensions"] if d["name"] == "order__status")
    assert "delivered" in status["values"] and "None" not in status["values"]


def test_group_order_limit(layer):
    out = layer.query(Query(["revenue"], group_by=["product__category"], order_by=["-revenue"], limit=1))
    assert out["columns"] == ["product__category", "revenue"] and len(out["rows"]) == 1
    assert "LIMIT 1" in out["sql"] and "--" not in out["sql"]


def test_truncation_flag(layer):
    out = layer.query(Query(["orders"], group_by=["metric_time__day", "order__state"]))
    assert len(out["rows"]) == 1000 and out["truncated"] is True


@pytest.mark.parametrize(
    "query, message",
    [
        (Query(["profit"]), "Unknown metric"),
        (Query([]), "at least one metric"),
        (Query(["revenue"], group_by=["payment__payment_type"]), "Cannot group by"),
        (Query(["revenue"], group_by=["metric_time__hour"]), "grain"),
        (Query(["revenue"], filters=[Filter("metric_time", ">", "2017-01-01")]), "start and end"),
        (Query(["revenue"], filters=[Filter("order__status", "like", "%")]), "Operator"),
        (Query(["revenue"], filters=[Filter("order__state", "in", [])]), "non-empty list"),
        (Query(["revenue"], filters=[Filter("order__state", "=", "{{ Dimension('x') }}")]), "braces"),
        (Query(["revenue"], filters=[Filter("order__state", "=", {"a": 1})]), "must be text"),
        (Query(["revenue"], start="2018-02-30"), "date like"),
        (Query(["revenue"], start="2018-02-01", end="2018-01-01"), "after end"),
        (Query(["revenue"], order_by=["orders"]), "order_by"),
        (Query(["revenue"], limit=5000), "limit"),
    ],
)
def test_rejects(layer, query, message):
    with pytest.raises(QueryError, match=message):
        layer.query(query)


def test_quotes_are_escaped_not_executed(layer):
    out = layer.query(Query(["revenue"], filters=[Filter("order__state", "=", "SP' or '1'='1")]))
    assert out["rows"] == [[None]]
    assert "'SP'' or ''1''=''1'" in out["sql"]


def test_database_is_read_only(layer):
    client = layer._mf._sql_client
    for stmt in ["create table main.x as select 1", "select * from read_csv('/etc/hosts')"]:
        with pytest.raises(Exception):
            client.execute(stmt)


def test_metricflow_internal_api_still_exists():
    # core.py relies on this internal class; an upgrade that moves it must fail here, loudly.
    from dbt_metricflow.cli.cli_configuration import CLIConfiguration

    assert hasattr(CLIConfiguration, "setup") and hasattr(CLIConfiguration, "mf")


def test_grouped_results_are_ordered_by_default(layer):
    for _ in range(5):
        years = [r[0] for r in layer.query(Query(["revenue"], group_by=["metric_time__year"]))["rows"]]
        assert years == ["2016-01-01", "2017-01-01", "2018-01-01"]
