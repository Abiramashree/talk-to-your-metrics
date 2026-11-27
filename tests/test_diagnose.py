"""The held-out checks and the failure tagging, against the real semantic layer. No LLM."""

import json
import pathlib
import sys

import pytest
import yaml

from fakes import FakeLLM, call, final

BENCH = pathlib.Path(__file__).resolve().parents[1] / "bench"
sys.path.insert(0, str(BENCH))
from check_heldout import ANSWERS, to_query  # noqa: E402
from diagnose import diagnose  # noqa: E402
import run_heldout  # noqa: E402
import score_heldout  # noqa: E402

QUESTIONS = {q["id"]: q for q in yaml.safe_load((BENCH / "heldout.yaml").read_text())}
GOLD = json.loads(ANSWERS.read_text())


@pytest.fixture
def execute(layer):
    return lambda spec: layer.query(to_query(spec))["rows"]


def run_with(execute, args, rows=None, final_answer=None):
    """A saved run whose last query is `args`; the final answer copies its rows unless given."""
    got = execute(args)
    out = rows if rows is not None else got
    return {"trace": [{"tool": "query_metrics", "arguments": args, "result": {"rows": got}}],
            "final": final_answer or {"answer": "x", "columns": [], "rows": out}}


def tag(qid, run, execute):
    return diagnose(QUESTIONS[qid], GOLD.get(qid), run, execute)


def test_frozen_answers_cover_every_answerable_question():
    answerable = {i for i, q in QUESTIONS.items() if q["trap"] != "unanswerable"}
    assert set(GOLD) == answerable and len(QUESTIONS) == 30


def test_a_correct_run_has_no_tag(execute):
    d = tag("h08", run_with(execute, QUESTIONS["h08"]["layer"]), execute)
    assert d["correct"] and d["tag"] is None


def test_right_query_wrong_copy_is_a_copy_failure(execute):
    d = tag("h04", run_with(execute, QUESTIONS["h04"]["layer"], rows=[[1.0]]), execute)
    assert d["tag"] == "copy"


@pytest.mark.parametrize("qid, change, expected", [
    ("h04", {"metrics": ["payments"]}, "metric"),                                       # "sales" read as payments
    ("h01", {"start": "2018-10-01", "end": "2018-10-17"}, "dates"),                    # "last quarter" read as this one
    ("h14", {"filters": []}, "filter"),                                                # "outside SP" dropped
    ("h18", {"order_by": ["-average_review_score"]}, "ranking"),                       # lowest read as highest
    ("h22", {"group_by": ["metric_time__quarter"]}, "grouping"),                       # by month read as by quarter
    ("h01", {"metrics": ["payments"], "start": "2018-10-01", "end": "2018-10-17"}, "metric+dates"),
])
def test_a_wrong_query_is_traced_to_the_part_that_broke_it(execute, qid, change, expected):
    args = {**QUESTIONS[qid]["layer"], **change}
    assert tag(qid, run_with(execute, args), execute)["tag"] == expected


def test_an_equivalent_date_range_is_not_a_fault(execute):
    # 2018 as start 2018-01-01 with no end gives the same rows as the reference; nothing is wrong
    args = {**QUESTIONS["h04"]["layer"], "end": None}
    assert tag("h04", run_with(execute, args), execute)["correct"]


def test_run_level_tags(execute):
    assert tag("h04", {"final": None, "error": "provider error: 503"}, execute)["tag"] == "provider_error"
    assert tag("h04", {"final": None, "error": "no valid final answer"}, execute)["tag"] == "no_answer"
    assert tag("h04", {"final": {"cannot_answer": "x"}}, execute)["tag"] == "wrong_decline"
    assert tag("h04", {"final": {"answer": "x", "columns": [], "rows": [[1]]}, "trace": []}, execute)["tag"] == "no_query"
    assert tag("h27", {"final": {"answer": "x", "columns": [], "rows": [[1]]}}, execute)["tag"] == "answered_unanswerable"
    assert tag("h27", {"final": {"cannot_answer": "no sellers"}}, execute)["correct"]


def test_without_a_reference_query_a_wrong_query_is_untraced():
    q = {**QUESTIONS["h04"], "layer": None}
    run = {"trace": [{"tool": "query_metrics", "arguments": {"metrics": ["payments"]}, "result": {"rows": [[1.0]]}}],
           "final": {"answer": "x", "columns": [], "rows": [[1.0]]}}
    assert diagnose(q, GOLD["h04"], run)["tag"] == "query"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_heldout_runs_are_saved_scored_and_marked_stale_when_the_prompt_changes(tmp_path, execute):
    args = QUESTIONS["h23"]["layer"]
    script = [call("query_metrics", args), final({"answer": "x", "columns": ["orders"], "rows": GOLD["h23"]})]

    def make():
        llm = FakeLLM(list(script))
        llm.model = "openai/gpt-oss-120b"
        return llm

    out = tmp_path / "C"
    result = await run_heldout.run_questions(run_heldout.questions(["h23"]), "C", out, tmp_path / "log", make)
    assert result["done"] == 1
    runs = {"h23": json.loads((out / "h23.json").read_text())}
    current = runs["h23"]["fingerprint"]
    s = score_heldout.summarise(run_heldout.questions(), GOLD, runs, current, execute)
    assert (s["scored"], s["correct"], s["stale"], s["failures"]) == (1, 1, 0, {})
    assert score_heldout.summarise(run_heldout.questions(), GOLD, runs, "another", execute)["stale"] == 1
