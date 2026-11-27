"""Run the conversations in chat_checks.yaml through the web chat's setup, turn by turn.

    python bench/check_chat.py            # runs what isn't saved yet
    python bench/check_chat.py --force    # reruns everything

Each turn gets the previous question and the query behind its answer, as the page sends them. Runs are
saved to bench/results/chat-checks/. Not part of the benchmark: written after it.
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

from check_app import expected, judge  # noqa: E402
from run import build  # noqa: E402
from ttym.agent import CHAT_SYSTEM, ask  # noqa: E402
from ttym.llm import LLM, LLMError, QuotaExhausted, load_env  # noqa: E402

OUT = HERE / "results" / "chat-checks"


def answered_query(run: dict) -> dict | None:
    """The query behind the answer: the last successful query_metrics call, as the page shows it."""
    for step in reversed(run.get("trace", [])):
        if step.get("tool") == "query_metrics" and isinstance(step.get("result"), dict) and "rows" in step["result"]:
            return step["arguments"]
    return None


async def main(force: bool) -> int:
    conversations = yaml.safe_load((HERE / "chat_checks.yaml").read_text())
    turns = [{"id": f"{c['id']}.{i + 1}", **t} for c in conversations for i, t in enumerate(c["turns"])]
    gold = expected(turns)
    server, _ = build("C")
    OUT.mkdir(parents=True, exist_ok=True)
    correct = 0
    for c in conversations:
        previous = None
        for i, turn in enumerate(c["turns"]):
            tid = f"{c['id']}.{i + 1}"
            path = OUT / f"{tid}.json"
            if path.exists() and not force:
                run = json.loads(path.read_text())
            else:
                started = time.time()
                try:
                    result = await ask(turn["question"], server, LLM(wait_budget=120), CHAT_SYSTEM, previous=previous)
                except QuotaExhausted:
                    print(f"stopped at {tid}: the daily model limit is used up; rerun later")
                    return 2
                except LLMError as e:
                    result = {"final": None, "error": f"provider error: {str(e)[:300]}",
                              "trace": getattr(e, "trace", []), "usage": getattr(e, "usage", {})}
                run = {"turn": tid, "question": turn["question"], "previous": previous,
                       "seconds": round(time.time() - started, 1), **result}
                path.write_text(json.dumps(run, indent=1, default=str) + "\n")
            ok, reason = judge(turn, gold.get(tid), run)
            correct += ok
            print(f"{tid}  {'ok   ' if ok else 'WRONG'}  {turn['question']!r}: {reason}")
            previous = {"question": turn["question"], "query": answered_query(run)}
    print(f"{correct}/{sum(len(c['turns']) for c in conversations)} turns correct")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    load_env(HERE.parent / ".env")
    sys.exit(asyncio.run(main(args.force)))
