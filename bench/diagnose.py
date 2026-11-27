"""Why a run went wrong, decided by code: no LLM, no judgment.

Every wrong run gets one tag. Run-level tags come from the run itself. When the question has a reference
semantic-layer query (heldout.yaml `layer`), a wrong query is traced to the part that broke it: each part of
the agent's last query (metric, grouping, filters, dates, ranking) is put into the reference query on its own
and executed; a part that makes the reference query wrong is at fault.

| Tag | Meaning | Context to look at |
|---|---|---|
| provider_error | the model provider failed | none (infrastructure) |
| no_answer | no valid final answer | answer format, step budget |
| wrong_decline | declined an answerable question | metric descriptions, not_available hints |
| answered_unanswerable | answered a question no metric answers | decline rules, level-of-detail reasons |
| no_query | gave rows without a successful query | tool-use rules |
| copy | the query was right; the final rows were not | answer format |
| metric | wrong metric | metric descriptions, synonyms |
| grouping | wrong group_by | dimension names, time grains |
| filter | wrong filter | dimension values |
| dates | wrong start / end | time rules (today, inclusive end) |
| ranking | wrong order_by / limit | ranking instructions |
| metric+dates, ... | several parts each break it on their own | each part named |
| combination | no single part breaks it, together they do | the query as a whole |
| query | wrong query, no reference to trace it against | the query as a whole |
"""

from __future__ import annotations

import copy

from score import rows_match

PARTS = {"metric": ("metrics",), "grouping": ("group_by",), "filter": ("filters",),
         "dates": ("start", "end"), "ranking": ("order_by", "limit")}


def last_query(run: dict) -> tuple[dict, list] | None:
    """The agent's last successful query_metrics call and its rows."""
    for step in reversed(run.get("trace", [])):
        result = step.get("result")
        if step.get("tool") == "query_metrics" and isinstance(result, dict) and "rows" in result:
            return step["arguments"], result["rows"]
    return None


def judge(question: dict, gold: list | None, run: dict) -> tuple[bool, str]:
    """The benchmark's rules: rows must match; a should-decline question must be declined."""
    final = run.get("final")
    if not isinstance(final, dict):
        return False, run.get("error") or "no final answer"
    if question["trap"] == "unanswerable":
        ok = "cannot_answer" in final and "rows" not in final
        return ok, "declined" if ok else "answered a question no metric answers"
    if "cannot_answer" in final:
        return False, "declined an answerable question"
    return rows_match(gold, question["kinds"], final.get("rows"), top_row_only=question.get("top_row", False))


def diagnose(question: dict, gold: list | None, run: dict, execute=None) -> dict:
    """{"correct", "reason", "tag", "faults"}. execute(spec) -> rows, or raises; needed to trace query faults."""
    ok, reason = judge(question, gold, run)
    out = {"correct": ok, "reason": reason, "tag": None, "faults": []}
    if ok:
        return out
    final = run.get("final")
    if not isinstance(final, dict):
        out["tag"] = "provider_error" if str(run.get("error", "")).startswith("provider error") else "no_answer"
        return out
    if question["trap"] == "unanswerable":
        out["tag"] = "answered_unanswerable"
        return out
    if "cannot_answer" in final:
        out["tag"] = "wrong_decline"
        return out
    found = last_query(run)
    if found is None:
        out["tag"] = "no_query"
        return out
    args, rows = found
    if rows_match(gold, question["kinds"], rows, top_row_only=question.get("top_row", False))[0]:
        out["tag"] = "copy"
        return out
    reference = question.get("layer")
    if reference is None or execute is None:
        out["tag"] = "query"
        return out
    for part, keys in PARTS.items():
        if all(_same(args.get(k), reference.get(k)) for k in keys):
            continue
        spec = copy.deepcopy(reference)
        for k in keys:
            spec[k] = args.get(k)
        try:
            broken = not rows_match(gold, question["kinds"], execute(spec), top_row_only=question.get("top_row", False))[0]
        except Exception:  # the swapped-in part makes the query invalid: that part is at fault
            broken = True
        if broken:
            out["faults"].append(part)
    out["tag"] = out["faults"][0] if len(out["faults"]) == 1 else "combination" if not out["faults"] else "+".join(out["faults"])
    return out


def _same(a, b) -> bool:
    return (a or None) == (b or None)
