"""Run benchmark questions through one setup and save every run.

    python bench/run.py --setup C --split dev            # all dev questions through setup C
    python bench/run.py --setup A --split test --ids t01 t07

Setups: A = raw text-to-SQL, A+ = A with the business definitions in the prompt, B = semantic layer without descriptions, C = semantic layer modeled for agents.
A run is skipped when a saved run exists for the same question and the same prompt fingerprint, so
reruns only touch what changed. A used-up daily limit stops the run; rerun the same command later.
Any other provider error is saved as a failed run (scored wrong) and is not retried. Every line printed
is also appended to bench/results/logs/<split>-<setup>.log.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import pathlib
import sys
import time

import yaml
from mcp import Client

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ttym.agent import SYSTEM as SEMANTIC_SYSTEM, ask  # noqa: E402
from ttym.llm import LLM, LLMError, QuotaExhausted, load_env  # noqa: E402

SETUPS = ("A", "A+", "B", "C")


def build(setup: str):
    """The MCP server and system prompt for a setup."""
    if setup in ("A", "A+"):
        from ttym.sql_server import SYSTEM, build_sql_server
        return build_sql_server(), SYSTEM if setup == "A" else SYSTEM + "\n\n" + definitions()
    from ttym.core import SemanticLayer
    from ttym.mcp_server import build_server
    return build_server(SemanticLayer(bare=setup == "B")), SEMANTIC_SYSTEM


def definitions() -> str:
    """The business definitions from bench/definitions.md, from the first section on (the intro names files A+ can't see)."""
    text = (ROOT / "bench" / "definitions.md").read_text()
    return "Business definitions every answer must follow:\n\n" + text[text.index("## Time"):].strip()


async def fingerprint(setup: str, server, system: str, model: str) -> str:
    """Everything the model sees before the question: prompt, tool schemas, and what discovery returns."""
    async with Client(server) as client:
        tools = [{"name": t.name, "description": t.description, "schema": t.input_schema}
                 for t in (await client.list_tools()).tools]
        first = "get_schema" if setup.startswith("A") else "list_metrics"
        discovery = (await client.call_tool(first, {})).structured_content
    blob = json.dumps({"setup": setup, "model": model, "system": system, "tools": tools, "discovery": discovery},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


async def run(setup: str, split: str, ids: list[str] | None, out_root: pathlib.Path, llm_factory=LLM, force=False) -> dict:
    questions = [q for q in yaml.safe_load((ROOT / "bench" / "questions.yaml").read_text())
                 if q["split"] == split and (not ids or q["id"] in ids)]
    server, system = build(setup)
    model = llm_factory().model
    key = await fingerprint(setup, server, system, model)
    out = out_root / f"runs-{split}" / setup
    out.mkdir(parents=True, exist_ok=True)
    done, skipped, totals = 0, 0, {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0, "requests": 0}
    logs = out_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logfile = logs / f"{split}-{setup}.log"

    def log(line: str) -> None:
        print(line)
        with logfile.open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {line}\n")

    log(f"start setup {setup} split {split} fingerprint {key}")
    for q in questions:
        path = out / f"{q['id']}.json"
        if path.exists() and not force and json.loads(path.read_text()).get("fingerprint") == key:
            skipped += 1
            continue
        llm = llm_factory()
        started = time.time()
        try:
            result = await ask(q["question"], server, llm, system)
        except QuotaExhausted:
            log(f"stopped at {q['id']}: the daily model limit is used up; rerun later to continue")
            return {"done": done, "skipped": skipped, "stopped": q["id"], **totals}
        except LLMError as e:
            result = {"final": None, "error": f"provider error: {str(e)[:300]}",
                      "trace": getattr(e, "trace", []), "usage": getattr(e, "usage", dict(llm.usage))}
        record = {"question_id": q["id"], "setup": setup, "model": model, "fingerprint": key,
                  "seconds": round(time.time() - started, 1), **result}
        path.write_text(json.dumps(record, indent=1, default=str) + "\n")
        for k in totals:
            totals[k] += result["usage"].get(k, 0)
        done += 1
        u = result["usage"]
        status = "answered" if result.get("final") is not None else f"failed: {result.get('error', '')[:200]}"
        log(f"{q['id']}  {u.get('requests', 0)} req  {u.get('prompt_tokens', 0)} in ({u.get('cached_tokens', 0)} cached)  "
            f"{u.get('completion_tokens', 0)} out  {record['seconds']}s  {status}")
    return {"done": done, "skipped": skipped, "stopped": None, **totals}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--setup", choices=SETUPS, required=True)
    parser.add_argument("--split", choices=("dev", "test"), required=True)
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--force", action="store_true", help="rerun even when a matching saved run exists")
    args = parser.parse_args()
    load_env(ROOT / ".env")
    summary = asyncio.run(run(args.setup, args.split, args.ids, ROOT / "bench" / "results",
                              llm_factory=lambda: LLM(wait_budget=120), force=args.force))
    counted = summary["prompt_tokens"] - summary["cached_tokens"] + summary["completion_tokens"]
    print(f"done {summary['done']}, skipped {summary['skipped']}; {summary['requests']} requests, "
          f"{summary['prompt_tokens']} in ({summary['cached_tokens']} cached), {summary['completion_tokens']} out; "
          f"~{counted} tokens counted against the daily limit")
    if summary["stopped"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
