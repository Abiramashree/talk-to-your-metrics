"""Ask the questions in app_checks.yaml through the app's setup (semantic layer modeled for agents).

    python bench/check_app.py            # runs what isn't saved yet for the current prompt and tools
    python bench/check_app.py --force    # reruns everything
    python bench/check_app.py --force --ids c6

Correct answers come from SQL on the raw tables, scored with the benchmark's matching rules.
Runs are saved to bench/results/app-checks/. Not part of the benchmark: written after it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time

import yaml

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

from run import build, fingerprint  # noqa: E402
from score import rows_match  # noqa: E402
from ttym.agent import ask  # noqa: E402
from ttym.llm import LLM, LLMError, QuotaExhausted, load_env  # noqa: E402

OUT = HERE / "results" / "app-checks"


def expected(questions: list[dict]) -> dict[str, list]:
    from ttym.sql_server import connect
    con = connect()
    try:
        return {q["id"]: [list(r) for r in con.execute(q["sql"]).fetchall()] for q in questions if q.get("sql")}
    finally:
        con.close()


def judge(q: dict, gold: list | None, run: dict) -> tuple[bool, str]:
    final = run.get("final")
    if not isinstance(final, dict):
        return False, run.get("error") or "no final answer"
    if q.get("decline"):
        ok = "cannot_answer" in final and "rows" not in final
        return ok, "declined" if ok else "answered a question no metric answers"
    if "cannot_answer" in final:
        return False, "declined an answerable question"
    return rows_match(gold, q["kinds"], final.get("rows"), top_row_only=q.get("top_row", False))


async def main(force: bool, ids: list[str] | None = None) -> int:
    questions = yaml.safe_load((HERE / "app_checks.yaml").read_text())
    gold = expected(questions)
    server, system = build("C")
    key = await fingerprint("C", server, system, LLM().model)
    OUT.mkdir(parents=True, exist_ok=True)
    correct = 0
    for q in questions:
        path = OUT / f"{q['id']}.json"
        saved = json.loads(path.read_text()) if path.exists() else None
        rerun = force and (not ids or q["id"] in ids)
        if saved and saved.get("fingerprint") == key and not rerun:
            run = saved
        else:
            started = time.time()
            try:
                result = await ask(q["question"], server, LLM(wait_budget=120), system)
            except QuotaExhausted:
                print(f"stopped at {q['id']}: the daily model limit is used up; rerun later")
                return 2
            except LLMError as e:
                result = {"final": None, "error": f"provider error: {str(e)[:300]}",
                          "trace": getattr(e, "trace", []), "usage": getattr(e, "usage", {})}
            run = {"question_id": q["id"], "question": q["question"], "fingerprint": key,
                   "seconds": round(time.time() - started, 1), **result}
            path.write_text(json.dumps(run, indent=1, default=str) + "\n")
        ok, reason = judge(q, gold.get(q["id"]), run)
        correct += ok
        tools = [s["tool"] for s in run.get("trace", [])]
        print(f"{q['id']}  {'ok   ' if ok else 'WRONG'}  {reason}  tools={tools}")
    print(f"{correct}/{len(questions)} correct")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--ids", nargs="*", help="with --force, rerun only these")
    args = parser.parse_args()
    load_env(HERE.parent / ".env")
    sys.exit(asyncio.run(main(args.force, args.ids)))
