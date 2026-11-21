"""Score benchmark runs by the rules in scoring.md. Pure functions; no network, no LLM.

    python bench/score.py            # scores bench/results/runs/* and writes bench/results/summary.json
"""

from __future__ import annotations

import json
import math
import pathlib
import sys
from collections import defaultdict

import yaml

HERE = pathlib.Path(__file__).resolve().parent
TOLERANCE = {"money": 0.005, "average": 0.005}
RATE_FRACTION, RATE_PERCENT = 0.00005, 0.005
TOP_ROW = {"t03", "t10"}  # "which ..." questions: only the top row's key must match


# ---------- matching ----------

def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not (isinstance(v, float) and math.isnan(v))


def number_matches(kind: str, expected: float, got) -> bool:
    if not _is_number(got):
        return False
    if kind == "count":
        return float(got) == float(expected)
    if kind == "rate":
        return abs(got - expected) <= RATE_FRACTION or abs(got - expected * 100) <= RATE_PERCENT
    return abs(got - expected) <= TOLERANCE[kind]


def key_matches(expected, got) -> bool:
    if got is None:
        return False
    if isinstance(expected, bool) or isinstance(got, bool):
        norm = lambda v: str(v).strip().lower() in ("true", "1", "yes")  # noqa: E731
        return isinstance(got, (bool, str, int)) and norm(expected) == norm(got)
    if isinstance(expected, int) and 1900 < expected < 2100:  # a year key matches a date in that year
        return str(got).strip()[:4] == str(expected)
    e, g = str(expected).strip(), str(got).strip()
    if len(e) >= 10 and e[4] == "-" and e[7] == "-":  # dates compare on YYYY-MM-DD
        return g[:10] == e[:10]
    return e.lower() == g.lower()


def row_matches(expected_row: list, kinds: list[str], got_row: list) -> bool:
    """Every expected key and value appears somewhere in the row (columns may be ordered differently)."""
    used: set[int] = set()
    for value, kind in zip(expected_row, kinds):
        found = None
        for i, cell in enumerate(got_row):
            if i in used:
                continue
            if (kind == "key" and key_matches(value, cell)) or (kind != "key" and number_matches(kind, value, cell)):
                found = i
                break
        if found is None:
            return False
        used.add(found)
    return True


def _empty_key(row: list) -> bool:
    """A row whose key is empty: it has an empty cell and no non-empty text, date or true/false cell."""
    return any(c is None for c in row) and all(c is None or _is_number(c) for c in row)


def rows_match(expected_rows: list, kinds: list[str], got_rows: list, top_row_only: bool = False) -> tuple[bool, str]:
    if not isinstance(got_rows, list) or not all(isinstance(r, list) for r in got_rows):
        return False, "rows are not a list of lists"
    if top_row_only:
        if not got_rows:
            return False, "no rows"
        return (True, "top row matches") if row_matches(expected_rows[0], kinds, got_rows[0]) else (False, "top row differs")
    remaining = list(range(len(got_rows)))
    for exp in expected_rows:
        hit = next((i for i in remaining if row_matches(exp, kinds, got_rows[i])), None)
        if hit is None:
            return False, f"expected row {exp} not found"
        remaining.remove(hit)
    has_keys = "key" in kinds
    extra = [got_rows[i] for i in remaining if not (has_keys and _empty_key(got_rows[i]))]
    if extra:
        return False, f"{len(extra)} unexpected row(s), e.g. {extra[0]}"
    return True, "all rows match"


def score_run(question: dict, expected: dict | None, kinds: list[str] | None, run: dict) -> dict:
    final = run.get("final")
    if not isinstance(final, dict):
        return {"correct": False, "reason": run.get("error") or "no final answer"}
    if question["trap"] == "unanswerable":
        ok = "cannot_answer" in final and "rows" not in final
        return {"correct": ok, "reason": "declined" if ok else "answered an unanswerable question"}
    if "cannot_answer" in final:
        return {"correct": False, "reason": "declined an answerable question"}
    ok, reason = rows_match(expected["rows"], kinds, final.get("rows"), top_row_only=question["id"] in TOP_ROW)
    return {"correct": ok, "reason": reason}


# ---------- copy fidelity ----------

def tool_rows(run: dict) -> list | None:
    """Rows of the last successful query (B, C) or executed SQL (A)."""
    for step in reversed(run.get("trace", [])):
        result = step.get("result")
        if step.get("tool") in ("query_metrics", "run_sql") and isinstance(result, dict) and "rows" in result:
            return result["rows"]
    return None


def _cell_equal(a, b) -> bool:
    if _is_number(a) and _is_number(b):
        return abs(a - b) <= max(0.005, 1e-9 * abs(a))
    if a is None or b is None:
        return a is b
    return str(a).strip()[:19].lower() == str(b).strip()[:19].lower()


def copy_differs(run: dict) -> bool | None:
    """None when there is nothing to compare (declined, or no successful query)."""
    final, source = run.get("final"), tool_rows(run)
    if not isinstance(final, dict) or "rows" not in final or source is None:
        return None
    copied = final["rows"]
    if not isinstance(copied, list) or len(copied) != len(source):
        return True
    pool = [list(r) for r in source]
    for row in copied:
        if not isinstance(row, list):
            return True
        hit = next((i for i, r in enumerate(pool) if len(r) == len(row) and all(_cell_equal(x, y) for x, y in zip(row, r))), None)
        if hit is None:
            return True
        pool.pop(hit)
    return False


# ---------- reporting ----------

def wilson(correct: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = correct / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def summarise(questions: list[dict], answers: dict, kinds: dict, runs: dict[str, dict[str, dict]], split: str = "test") -> dict:
    """runs: {setup: {question_id: run}}"""
    pool = [q for q in questions if q["split"] == split]
    out: dict = {"split": split, "setups": {}}
    for setup, by_id in sorted(runs.items()):
        rows, per_trap = [], defaultdict(lambda: [0, 0])
        differs = differs_wrong = 0
        for q in pool:
            run = by_id.get(q["id"])
            if run is None:
                continue
            s = score_run(q, answers.get(q["id"]), kinds.get(q["id"]), run)
            d = copy_differs(run)
            rows.append({"id": q["id"], "trap": q["trap"], **s, "copy_differs": d,
                         "tokens": run.get("usage", {}).get("prompt_tokens", 0) + run.get("usage", {}).get("completion_tokens", 0)})
            per_trap[q["trap"]][0] += s["correct"]
            per_trap[q["trap"]][1] += 1
            if d:
                differs += 1
                differs_wrong += not s["correct"]
        n, correct = len(rows), sum(r["correct"] for r in rows)
        low, high = wilson(correct, n)
        out["setups"][setup] = {
            "scored": n, "of": len(pool), "correct": correct,
            "accuracy": round(correct / n, 4) if n else None, "wilson95": [round(low, 4), round(high, 4)],
            "per_trap": {t: {"correct": c, "n": k} for t, (c, k) in sorted(per_trap.items())},
            "copy_differs": differs, "copy_differs_and_wrong": differs_wrong,
            "questions": rows,
        }
    return out


def load_runs(root: pathlib.Path) -> dict[str, dict[str, dict]]:
    runs: dict[str, dict[str, dict]] = {}
    for path in sorted(root.glob("*/*.json")):
        runs.setdefault(path.parent.name, {})[path.stem] = json.loads(path.read_text())
    return runs


def main() -> None:
    split = sys.argv[1] if len(sys.argv) > 1 else "test"
    questions = yaml.safe_load((HERE / "questions.yaml").read_text())
    answers = json.loads((HERE / "answers.json").read_text())
    kinds = yaml.safe_load((HERE / "scoring.yaml").read_text())
    summary = summarise(questions, answers, kinds, load_runs(HERE / "results" / f"runs-{split}"), split)
    out = HERE / "results" / f"summary-{split}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2) + "\n")
    for setup, s in summary["setups"].items():
        lo, hi = s["wilson95"]
        print(f"{setup}: {s['correct']}/{s['scored']} (of {s['of']})  95% CI {lo:.2f}–{hi:.2f}  copy differs: {s['copy_differs']}")


if __name__ == "__main__":
    main()
