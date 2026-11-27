"""The harness: context selection, verified queries and answer checks. Real semantic layer, scripted model."""

import glob
import json
import pathlib
import sys

import pytest
import yaml

from fakes import FakeLLM, call, final
from ttym.harness import HARNESS_SYSTEM, VERIFIED, AnswerCheck, Harness, harness_ask, terms
from ttym.mcp_server import build_server

ROOT = pathlib.Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench"
sys.path.insert(0, str(BENCH))
from check_heldout import to_query  # noqa: E402
from score import rows_match  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def harness(layer):
    return Harness(layer)


def _yaml(name):
    return yaml.safe_load((BENCH / name).read_text())


# ---------- part 1: context selection ----------

def _pre_heldout_questions():
    """Questions written before the held-out set, with the metric setup C's correct run used."""
    text = {q["id"]: q["question"] for q in _yaml("questions.yaml") + _yaml("app_checks.yaml")}
    out = []
    for path in sorted(glob.glob(str(BENCH / "results/runs-*/C/*.json")) + glob.glob(str(BENCH / "results/app-checks/c*.json"))):
        run = json.loads(pathlib.Path(path).read_text())
        used = [s["arguments"]["metrics"] for s in run["trace"]
                if s["tool"] == "query_metrics" and isinstance(s.get("result"), dict) and "rows" in s["result"]]
        if used:
            out.append((text[run["question_id"]], used[-1]))
    return out


def test_selection_keeps_the_right_metric_for_every_pre_heldout_question(harness):
    items = _pre_heldout_questions()
    assert len(items) == 35
    assert [(q, m) for q, m in items if not set(m) <= set(harness.select_metrics(q))] == []


def test_a_named_category_brings_in_a_metric_that_works_with_categories(harness):
    q = "How many orders placed in 2017 included something from computers_accessories?"
    assert harness.mentioned_values(q) == [("product__category", "computers_accessories")]
    assert "orders_by_category" in harness.select_metrics(q)


def test_named_values(harness):
    assert harness.mentioned_values("How many cancelled orders in RJ paid by boleto?") == [
        ("order__state", "RJ"), ("order__status", "canceled"), ("payment__payment_type", "boleto")]
    assert harness.mentioned_values("Is it so? Revenue by state") == []  # "so" is not the state code SO


def test_context_is_smaller_than_full_discovery(harness, layer):
    full = len(json.dumps(layer.list_metrics())) + len(json.dumps(layer.get_dimensions(["revenue"])))
    text = harness.context("Which product category brought in the most revenue in 2018?").text()
    assert "revenue:" in text and "product__category" in text and len(text) < full / 2


# ---------- part 2: verified queries ----------

def _gold():
    from check_app import expected
    gold = expected([{**c["turns"][0], "id": f"{c['id']}.1"} for c in _yaml("chat_checks.yaml")])
    gold.update(expected(_yaml("app_checks.yaml")))
    answers = json.loads((BENCH / "answers.json").read_text())
    gold.update({k: v["rows"] for k, v in answers.items() if k.startswith("d")})
    kinds = {q["id"]: q.get("kinds") for q in _yaml("app_checks.yaml")}
    kinds.update({f"{c['id']}.1": c["turns"][0].get("kinds") for c in _yaml("chat_checks.yaml")})
    kinds.update({k: v for k, v in _yaml("scoring.yaml").items() if k.startswith("d")})
    return json.loads(json.dumps(gold, default=lambda v: float(v) if hasattr(v, "as_tuple") else str(v))), kinds


def test_every_verified_query_gives_the_correct_answer(layer):
    library = yaml.safe_load(VERIFIED.read_text())
    gold, kinds = _gold()
    checked = 0
    for entry in library:
        if "query" not in entry:
            assert entry["cannot_answer"]
            continue
        source = entry["source"]
        rows = layer.query(to_query(entry["query"]))["rows"]
        top = source == "c3"  # "which" question: the top row
        ok, reason = rows_match(gold[source], kinds[source], rows, top_row_only=top)
        assert ok, (source, reason)
        checked += 1
    assert checked == 14 and len(library) == 19


def test_verified_queries_share_no_question_with_the_test_or_heldout_sets():
    library = [set(terms(e["question"])) for e in yaml.safe_load(VERIFIED.read_text())]
    scored = [q["question"] for q in _yaml("questions.yaml") if q["split"] == "test"] + [q["question"] for q in _yaml("heldout.yaml")]

    def jaccard(a, b):
        return len(a & b) / len(a | b)

    near = [(q, round(max(jaccard(set(terms(q)), lib) for lib in library), 2)) for q in scored]
    assert [n for n in near if n[1] >= 0.8] == []


def test_similar_examples_come_from_the_library(harness):
    examples = harness.similar_examples("How many canceled orders contained a toys item?")
    assert examples[0]["source"] == "c1"


# ---------- part 3: answer checks ----------

def _trace(args, rows, columns=("x",)):
    return [{"tool": "query_metrics", "arguments": args, "result": {"columns": list(columns), "rows": rows}}]


def test_a_changed_number_is_replaced_by_the_query_rows(harness):
    check = AnswerCheck(harness, "What was revenue in 2017?")
    args = {"metrics": ["revenue"], "start": "2017-01-01", "end": "2017-12-31"}
    out = check({"answer": "x", "columns": ["revenue"], "rows": [[1.0]]}, _trace(args, [[6000000.5]], ["revenue"]))
    assert out["final"]["rows"] == [[6000000.5]] and out["found"] == ["copy"] and out["feedback"] is None


def test_keeping_only_some_rows_is_not_a_copy_error(harness):
    check = AnswerCheck(harness, "Which state placed the most orders?")
    args = {"metrics": ["orders"], "group_by": ["order__state"], "order_by": ["-orders"]}
    out = check({"answer": "x", "columns": [], "rows": [["SP", 3]]}, _trace(args, [["SP", 3], ["RJ", 2]]))
    assert out["found"] == [] and out["feedback"] is None


@pytest.mark.parametrize("question, args, rows, expected", [
    ("Revenue in 2017?", {"metrics": ["revenue"]}, [[1]], "dates"),
    ("Revenue in 2017?", {"metrics": ["revenue"], "start": "2018-01-01", "end": "2018-12-31"}, [[1]], "dates"),
    ("What was revenue last quarter?", {"metrics": ["revenue"]}, [[1]], "dates"),
    ("Which state had the most orders in 2017?", {"metrics": ["orders"], "group_by": ["order__state"],
                                                   "start": "2017-01-01", "end": "2017-12-31"}, [["SP", 1]], "ranking"),
    ("Top 3 states by orders in 2017", {"metrics": ["orders"], "group_by": ["order__state"], "order_by": ["-orders"],
                                        "limit": 5, "start": "2017-01-01", "end": "2017-12-31"}, [["SP", 1]], "ranking"),
    ("Revenue from SP in 2018", {"metrics": ["revenue"], "start": "2018-01-01", "end": "2018-12-31"}, [[1]], "values"),
    ("Compare orders in RJ and MG in 2018", {"metrics": ["orders"], "group_by": ["order__state"],
                                             "start": "2018-01-01", "end": "2018-12-31"}, [["RJ", 1], ["MG", 2], ["SP", 3]], "values"),
    ("Canceled orders in 2018", {"metrics": ["canceled_orders"], "start": "2018-01-01", "end": "2018-12-31"}, [], "empty"),
])
def test_checks_find_problems_and_ask_for_a_fix(harness, question, args, rows, expected):
    out = AnswerCheck(harness, question)({"answer": "x", "columns": [], "rows": rows}, _trace(args, rows))
    assert expected in out["found"] and out["feedback"]


@pytest.mark.parametrize("question, args", [
    ("How many orders were delivered in 2017?", {"metrics": ["delivered_orders"], "start": "2017-01-01", "end": "2017-12-31"}),
    ("Revenue outside SP in 2018", {"metrics": ["revenue"], "start": "2018-01-01", "end": "2018-12-31",
                                     "filters": [{"dimension": "order__state", "operator": "!=", "value": "SP"}]}),
    ("What is the overall late delivery rate?", {"metrics": ["late_delivery_rate"]}),
    ("Revenue for SP and RJ by state in 2017", {"metrics": ["revenue"], "group_by": ["order__state"], "start": "2017-01-01",
                                                 "end": "2017-12-31", "filters": [{"dimension": "order__state", "operator": "in", "value": ["SP", "RJ"]}]}),
])
def test_correct_queries_pass_without_feedback(harness, question, args):
    out = AnswerCheck(harness, question)({"answer": "x", "columns": [], "rows": [[1]]}, _trace(args, [[1]]))
    assert out["found"] == [] and out["feedback"] is None


def test_a_decline_is_not_checked(harness):
    out = AnswerCheck(harness, "Revenue in 2017 by seller?")({"cannot_answer": "no sellers"}, [])
    assert out == {"final": {"cannot_answer": "no sellers"}, "feedback": None, "found": []}


# ---------- the loop ----------

async def test_feedback_gives_the_model_one_chance_to_fix_the_query(harness, layer):
    q = "What was revenue in 2017?"
    good = {"metrics": ["revenue"], "start": "2017-01-01", "end": "2017-12-31"}
    expected = layer.query(to_query(good))["rows"]
    llm = FakeLLM([call("query_metrics", {"metrics": ["revenue"]}),
                   final({"answer": "x", "columns": ["revenue"], "rows": layer.query(to_query({"metrics": ["revenue"]}))["rows"]}),
                   call("query_metrics", good, id="c2"),
                   final({"answer": "x", "columns": ["revenue"], "rows": expected})])
    out = await harness_ask(q, build_server(layer), harness, llm)
    assert out["final"]["rows"] == expected
    assert out["checks"][0]["found"] == ["dates"] and len(out["checks"]) == 1
    assert "names 2017" in llm.sent[2]["messages"][-1]["content"]
    assert llm.sent[0]["messages"][0]["content"] == HARNESS_SYSTEM
    assert "Metrics most likely to fit" in llm.sent[0]["messages"][1]["content"] and out["harness"]["metrics"][0] == "revenue"


async def test_without_a_check_the_agent_is_unchanged(layer):
    # Setup C: same prompt, tools and discovery as when the app checks were saved, so its fingerprint matches.
    from run import build, fingerprint
    server, system = build("C")
    saved = json.loads((BENCH / "results/app-checks/c1.json").read_text())["fingerprint"]
    assert await fingerprint("C", server, system, "openai/gpt-oss-120b") == saved
