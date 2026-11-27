"""Score the saved held-out runs and tag every wrong one (bench/diagnose.py). No LLM calls.

    python bench/score_heldout.py

Writes bench/results/heldout/summary.json and prints accuracy, tokens and the failure table per setup.
Runs saved under an older prompt or tool fingerprint are reported as stale, not silently mixed in.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from collections import Counter

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

from check_heldout import ANSWERS, to_query  # noqa: E402
from diagnose import diagnose  # noqa: E402
from run_heldout import RESULTS, SETUPS, questions  # noqa: E402
from score import wilson  # noqa: E402

OUT = RESULTS / "heldout"


async def current_fingerprint(setup: str) -> str:
    from run import build, fingerprint
    from ttym.llm import MODEL
    server, system = build(setup)
    return await fingerprint(setup, server, system, MODEL)


def tokens(run: dict) -> int:
    u = run.get("usage", {})
    return u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)


def summarise(qs: list[dict], gold: dict, runs: dict[str, dict], current: str | None, execute) -> dict:
    rows = []
    for q in qs:
        run = runs.get(q["id"])
        if run is None:
            continue
        d = diagnose(q, gold.get(q["id"]), run, execute)
        rows.append({"id": q["id"], "trap": q["trap"], **d, "tokens": tokens(run),
                     "requests": run.get("usage", {}).get("requests", 0), "seconds": run.get("seconds"),
                     "stale": current is not None and run.get("fingerprint") != current})
    n, correct = len(rows), sum(r["correct"] for r in rows)
    low, high = wilson(correct, n)
    measured = [r["tokens"] for r in rows if r["tokens"]]
    return {"scored": n, "of": len(qs), "correct": correct, "wilson95": [round(low, 4), round(high, 4)],
            "stale": sum(r["stale"] for r in rows),
            "mean_tokens": round(sum(measured) / len(measured)) if measured else None,
            "failures": dict(Counter(r["tag"] for r in rows if not r["correct"]).most_common()),
            "questions": rows}


def main() -> int:
    from ttym.core import SemanticLayer
    layer = SemanticLayer()
    execute = lambda spec: layer.query(to_query(spec))["rows"]  # noqa: E731
    qs, gold = questions(), json.loads(ANSWERS.read_text())
    summary = {}
    for setup in SETUPS:
        folder = OUT / setup
        runs = {p.stem: json.loads(p.read_text()) for p in sorted(folder.glob("h*.json"))} if folder.exists() else {}
        s = summarise(qs, gold, runs, asyncio.run(current_fingerprint(setup)), execute)
        summary[setup] = s
        lo, hi = s["wilson95"]
        print(f"{setup}: {s['correct']}/{s['scored']} (of {s['of']})  95% CI {lo:.2f}-{hi:.2f}  "
              f"mean tokens {s['mean_tokens']}  stale {s['stale']}")
        for tag, count in s["failures"].items():
            print(f"   {count}  {tag}")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
