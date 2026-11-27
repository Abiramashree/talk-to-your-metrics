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
    assert len(names) == 20 and {"revenue", "repeat_customers", "late_delivery_rate"} <= names
    revenue = next(m for m in out["metrics"] if m["name"] == "revenue")
    assert "Excludes freight" in revenue["description"] and "sales" in revenue["synonyms"]
    assert out["today"] == "2018-10-17"
    assert "new_customers" in names and "all_customers" not in names


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
        (Query(["repeat_customers"], start="2017-01-01", end="2017-12-31"), "over all time"),
        (Query(["repeat_customer_rate"], group_by=["metric_time__year"]), "over all time"),
        (Query(["repeat_customers", "orders"], end="2017-12-31"), "over all time"),
        (Query(["revenue"], filters=[Filter("order__is_late", "=", "banana")]), "Could not convert string 'banana' to BOOL"),
        (Query(["revenue"], filters=[Filter("order__review_score", ">", "abc")]), "Could not convert string 'abc'"),
        (Query(["revenue"], filters=[Filter("order__state", "=", float("nan"))]), "finite"),
        (Query(["revenue"], filters=[Filter("order__state", "=", float("inf"))]), "finite"),
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


def test_all_time_metrics_without_dates(layer):
    assert layer.query(Query(["repeat_customers"]))["rows"] == [[2997]]
    assert layer.query(Query(["new_customers"]))["rows"] == [[96096]]


def test_new_customers_differ_from_customers_in_a_period(layer):
    year = dict(start="2017-01-01", end="2017-12-31")
    assert layer.query(Query(["new_customers"], **year))["rows"] == [[43708]]
    assert layer.query(Query(["customers"], **year))["rows"] == [[43713]]


def test_scoring_kinds_cover_every_expected_answer(answers):
    import pathlib

    import yaml

    kinds = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "bench" / "scoring.yaml").read_text())
    assert set(kinds) == set(answers)
    for qid, a in answers.items():
        assert len(kinds[qid]) == len(a["columns"]), qid
        assert set(kinds[qid]) <= {"key", "count", "money", "average", "rate"}


def test_cross_level_dimensions_are_explained(layer):
    from ttym.core import Filter, Query, QueryError
    d = layer.get_dimensions(["canceled_orders"])
    category = next(u for u in d["not_available"] if u["name"] == "product__category")
    assert "several categories" in category["why"] and "orders_by_category" in category["works_with"]
    assert "canceled_orders" not in category["works_with"]
    with pytest.raises(QueryError, match="several categories.*orders_by_category"):
        layer.query(Query(metrics=["canceled_orders"], group_by=["product__category"]))
    with pytest.raises(QueryError, match="paid in several ways.*orders_by_payment_type"):
        layer.query(Query(metrics=["orders"], filters=[Filter(dimension="payment__payment_type", operator="=", value="boleto")]))
    with pytest.raises(QueryError, match="over all time"):
        layer.query(Query(metrics=["repeat_customers"], group_by=["order__state"]))
    bare = layer.view(bare=True).get_dimensions(["canceled_orders"])
    assert all("why" not in u for u in bare["not_available"])


def test_orders_by_category_and_payment_type_match_raw_sql(layer):
    from ttym.core import Filter, Query
    canceled = layer.query(Query(metrics=["orders_by_category"], filters=[
        Filter(dimension="order__status", operator="=", value="canceled"),
        Filter(dimension="product__category", operator="=", value="health_beauty")]))
    assert canceled["rows"] == [[36]]
    paid = layer.query(Query(metrics=["orders_by_payment_type"], group_by=["payment__payment_type"],
                             filters=[Filter(dimension="order__status", operator="=", value="delivered")]))
    assert dict(map(tuple, paid["rows"])) == {"boleto": 19191, "credit_card": 74304, "debit_card": 1485, "voucher": 3679}
