import json
import pathlib
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bench"))
from score import copy_differs, number_matches, rows_match, score_run, summarise, wilson  # noqa: E402

BENCH = pathlib.Path(__file__).resolve().parents[1] / "bench"
QUESTIONS = {q["id"]: q for q in yaml.safe_load((BENCH / "questions.yaml").read_text())}
ANSWERS = json.loads((BENCH / "answers.json").read_text())
KINDS = yaml.safe_load((BENCH / "scoring.yaml").read_text())


def run(final, trace=None):
    return {"final": final, "trace": trace or []}


def score(qid, final):
    return score_run(QUESTIONS[qid], ANSWERS.get(qid), KINDS.get(qid), run(final))["correct"]


def test_number_kinds():
    assert number_matches("count", 2997, 2997.0) and not number_matches("count", 2997, 2996)
    assert number_matches("money", 5962902.01, 5962902.010003) and not number_matches("money", 5962902.01, 5962902.02)
    assert number_matches("rate", 0.031188, 0.0312) and number_matches("rate", 0.031188, 3.1188)
    assert not number_matches("rate", 0.031188, 0.0313)
    assert not number_matches("count", 1, True)


def test_exact_answer_from_answers_json_scores_correct():
    for qid, expected in ANSWERS.items():
        assert score(qid, {"columns": expected["columns"], "rows": expected["rows"]}), qid


def test_month_keys_compare_on_dates():
    rows = [[r[0][:10], r[1]] for r in ANSWERS["t02"]["rows"]]  # layer returns dates, gold has timestamps
    assert score("t02", {"columns": ["month", "revenue"], "rows": rows})


def test_year_key_matches_a_date_in_that_year():
    rows = [["2016-01-01", 40470.98], ["2017-01-01", 5962902.01]]
    assert score("t22", {"columns": ["metric_time__year", "revenue"], "rows": rows})


def test_columns_may_be_reordered():
    assert score("t10", {"columns": ["orders", "state"], "rows": [[17760, "SP"]]})


def test_empty_key_rows_are_ignored_other_extra_rows_are_not():
    rows = ANSWERS["t20"]["rows"] + [[None, 1.749]]
    assert score("t20", {"columns": ["is_late", "average_review_score"], "rows": rows})
    assert not score("t22", {"columns": ["year", "revenue"],
                             "rows": [[2016, 40470.98], [2017, 5962902.01], [2018, 7218125.12]]})
    assert not score("t20", {"columns": ["is_late", "s"], "rows": ANSWERS["t20"]["rows"] + [["maybe", None]]})


def test_top_row_questions():
    assert score("t03", {"columns": ["category", "revenue"], "rows": [["health_beauty", 755724.5], ["watches_gifts", 1.0]]})
    assert score("t03", {"columns": ["category"], "rows": [["health_beauty"]]}) is False  # value missing: kinds need it
    assert not score("t03", {"columns": ["category", "revenue"], "rows": [["watches_gifts", 1.0]]})


def test_wrong_numbers_and_shapes():
    assert not score("t07", {"columns": ["x"], "rows": [[0]]})  # the customer_id trap
    assert not score("t01", {"columns": ["revenue"], "rows": [[13591643.7]]})
    assert not score("t01", {"columns": ["revenue"], "rows": "5962902.01"})


def test_unanswerable_rule_is_mechanical():
    assert score("u05", {"cannot_answer": "Returns aren't recorded; the cancellation rate was 0.6%."})
    assert not score("u05", {"answer": "x", "columns": ["cancellation_rate"], "rows": [[0.006]]})
    assert not score("u05", {"cannot_answer": "no", "rows": []})


def test_declining_an_answerable_question_is_wrong():
    assert not score("t01", {"cannot_answer": "not sure"})
    assert score_run(QUESTIONS["t01"], ANSWERS["t01"], KINDS["t01"], {"final": None, "error": "boom"})["correct"] is False


def test_copy_fidelity():
    trace = [{"tool": "query_metrics", "result": {"rows": [[2997]]}}]
    assert copy_differs(run({"columns": ["n"], "rows": [[2997]]}, trace)) is False
    assert copy_differs(run({"columns": ["n"], "rows": [[2998]]}, trace)) is True
    assert copy_differs(run({"columns": ["n"], "rows": [[2997], [1]]}, trace)) is True
    assert copy_differs(run({"cannot_answer": "x"}, trace)) is None
    assert copy_differs(run({"columns": ["n"], "rows": [[2997]]}, [])) is None
    sql = [{"tool": "run_sql", "result": {"rows": [["SP", 17760]]}}, {"tool": "run_sql", "result": {"error": "x"}}]
    assert copy_differs(run({"columns": ["s", "n"], "rows": [["SP", 17760]]}, sql)) is False


def test_wilson_interval():
    low, high = wilson(24, 30)
    assert 0.6 < low < 0.65 and 0.9 < high < 0.92
    assert wilson(0, 0) == (0.0, 0.0)


def test_summary_counts_per_setup_and_trap():
    runs = {"C": {"t07": run({"columns": ["n"], "rows": [[2997]]}, [{"tool": "query_metrics", "result": {"rows": [[2997]]}}]),
                  "u01": run({"cannot_answer": "No cost data."})}}
    s = summarise(list(QUESTIONS.values()), ANSWERS, KINDS, runs)["setups"]["C"]
    assert s["correct"] == 2 and s["scored"] == 2 and s["of"] == 30
    assert s["per_trap"]["customer-identity"] == {"correct": 1, "n": 1}
    assert s["copy_differs"] == 0
