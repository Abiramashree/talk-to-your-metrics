"""Run the held-out questions through a setup and save every run with its full trace.

    python bench/run_heldout.py --setup C                 # all 30, skipping runs saved for the same fingerprint
    python bench/run_heldout.py --setup H                 # the same with the harness
    python bench/run_heldout.py --setup C --ids h01 h25

Runs go to bench/results/heldout/<setup>/, the log to bench/results/logs/heldout-<setup>.log.
Each question costs about 3-6k tokens of the daily model limit. Score with bench/score_heldout.py.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

import yaml

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run import run_questions  # noqa: E402
from ttym.llm import LLM, load_env  # noqa: E402

SETUPS = ("C", "H")  # C = semantic layer modeled for agents; H = C with the harness
RESULTS = HERE / "results"


def questions(ids: list[str] | None = None) -> list[dict]:
    return [q for q in yaml.safe_load((HERE / "heldout.yaml").read_text()) if not ids or q["id"] in ids]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--setup", choices=SETUPS, required=True)
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--force", action="store_true", help="rerun even when a matching saved run exists")
    args = parser.parse_args()
    load_env(HERE.parent / ".env")
    summary = asyncio.run(run_questions(questions(args.ids), args.setup, RESULTS / "heldout" / args.setup,
                                        RESULTS / "logs" / f"heldout-{args.setup}.log",
                                        lambda: LLM(wait_budget=120), args.force, "held-out"))
    counted = summary["prompt_tokens"] - summary["cached_tokens"] + summary["completion_tokens"]
    print(f"done {summary['done']}, skipped {summary['skipped']}; ~{counted} tokens counted against the daily limit")
    if summary["stopped"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
