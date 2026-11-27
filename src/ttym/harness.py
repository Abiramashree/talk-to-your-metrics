"""The harness: code around the model that decides what context each question gets and checks the answer.

Three parts, all deterministic (no extra model calls):
1. Context selection: the metrics most likely to fit the question, their dimensions, and the dimension values
   the question names, given up front so the model can skip most discovery calls.
2. Verified queries: checked question -> query examples (context/verified_queries.yaml) similar to the question.
3. Answer checks: before an answer is returned, code checks it against the query results and the question,
   and gives the model one chance to fix what it finds.

The model still has every tool; when the selected context doesn't fit it can call list_metrics as before.
"""

from __future__ import annotations

import json
import math
import pathlib
import re
from collections import Counter
from dataclasses import dataclass, field

import yaml

from ttym.agent import SYSTEM, ask
from ttym.core import SemanticLayer

ROOT = pathlib.Path(__file__).resolve().parents[2]
VERIFIED = ROOT / "context" / "verified_queries.yaml"
TOP_METRICS = 5
TOP_EXAMPLES = 2
LISTED_VALUES = 12  # dimensions with at most this many values list them all; longer ones list only values the question names

STOP = set("""a an and are as at be by did do does for from had has have how i in is it its many much of on or our
our per show the their them there these this those to was we were what when which who with you your""".split())

HARNESS_SYSTEM = SYSTEM + """

Before the question, the harness gives you the metrics most likely to fit it, their dimensions, values the
question names, and verified queries for similar questions. Start from these: when a listed metric fits, go
straight to query_metrics. Call list_metrics or get_dimensions only if nothing listed fits."""


# ---------- text matching ----------

def _stem(word: str) -> str:
    word = word.replace("cancelled", "canceled")
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def terms(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower().replace("_", " ")) if w not in STOP]


class Index:
    """Weighted term overlap with inverse document frequency; phrases (synonyms, labels) score extra when present whole."""

    def __init__(self, docs: dict[str, list[tuple[str, float]]]) -> None:
        self.docs = {k: [(terms(t), w) for t, w in fields] for k, fields in docs.items()}
        df = Counter(t for fields in self.docs.values() for t in {t for ts, _ in fields for t in ts})
        self.idf = {t: math.log(1 + len(self.docs) / n) for t, n in df.items()}

    def rank(self, text: str) -> list[tuple[str, float]]:
        q = terms(text)
        joined, qset = " ".join(q), set(q)
        scores = {}
        for key, fields in self.docs.items():
            best: dict[str, float] = {}
            score = 0.0
            for ts, weight in fields:
                for t in set(ts) & qset:
                    best[t] = max(best.get(t, 0.0), weight * self.idf.get(t, 0.0))
                if ts and f" {' '.join(ts)} " in f" {joined} ":  # the whole name, label or synonym is in the question
                    score += weight * len(ts)
            scores[key] = score + sum(best.values())
        return sorted(((k, s) for k, s in scores.items() if s > 0), key=lambda kv: (-kv[1], kv[0]))


# ---------- part 1: context selection ----------

@dataclass
class Context:
    metrics: list[dict]
    dimensions: dict[str, dict]           # name -> {"description", "values"?, "metrics": [...]}
    mentioned: list[tuple[str, str]]      # (dimension, value) the question names
    examples: list[dict] = field(default_factory=list)

    def text(self) -> str:
        lines = ["Metrics most likely to fit (name: description; synonyms):"]
        for m in self.metrics:
            syn = f"; {', '.join(m['synonyms'])}" if m.get("synonyms") else ""
            lines.append(f"- {m['name']}: {m['description']}{syn}")
        lines.append("Dimensions (name: which of these metrics it works with; values):")
        for name, d in sorted(self.dimensions.items()):
            values = f"; values: {', '.join(d['values'])}" if d.get("values") else ""
            lines.append(f"- {name}: {', '.join(d['metrics'])}{values}")
        lines.append("Group by time with metric_time__day|week|month|quarter|year; limit dates with start/end (inclusive).")
        if self.mentioned:
            lines.append("Values the question names: " + "; ".join(f"{d} = {v}" for d, v in self.mentioned))
        if self.examples:
            lines.append("Verified queries for similar questions:")
            for e in self.examples:
                answer = f"query_metrics {e['query']}" if e.get("query") else f"cannot_answer: {e['cannot_answer']}"
                lines.append(f"- {e['question']} -> {answer}")
        return "\n".join(lines)


class Harness:
    def __init__(self, layer: SemanticLayer, verified: pathlib.Path = VERIFIED) -> None:
        self.layer = layer.view(bare=False)
        self.catalog = self.layer.list_metrics()["metrics"]
        self.metric_index = Index({
            m["name"]: [(m["name"], 3.0), (m.get("label", ""), 3.0), *((s, 2.0) for s in m.get("synonyms") or []),
                        (m.get("description", ""), 1.0)]
            for m in self.catalog})
        self.dims = {m["name"]: self.layer.get_dimensions([m["name"]])["dimensions"] for m in self.catalog}
        # Full value lists, for spotting values the question names (get_dimensions shows at most 30).
        # A metric with a built-in filter (canceled_orders) only sees some values, so take the union over metrics.
        self.values: dict[str, list[str]] = {}
        for metric, dims in self.dims.items():
            for d in dims:
                known = self.values.setdefault(d["name"], [])
                known += [v for v in self.layer.dimension_values(metric, d["name"]) if v not in known]
        self.examples = yaml.safe_load(verified.read_text()) if verified.exists() else []
        self.example_index = Index({str(i): [(e["question"], 1.0)] for i, e in enumerate(self.examples)})

    def description(self, metric: str) -> str | None:
        return next((m.get("description") for m in self.catalog if m["name"] == metric), None)

    def select_metrics(self, question: str, k: int = TOP_METRICS) -> list[str]:
        """The top-k metrics by term overlap. When the question names a value of a dimension none of them
        works with (a product category, a payment type), the best-ranked metric that does work with it is added."""
        ranked = [name for name, _ in self.metric_index.rank(question)]
        chosen = ranked[:k]
        for dim, _ in self.mentioned_values(question):
            if not any(dim in self._dim_names(m) for m in chosen):
                fit = next((m for m in ranked + sorted(self.dims) if dim in self._dim_names(m)), None)
                if fit and fit not in chosen:
                    chosen.append(fit)
        return chosen

    def _dim_names(self, metric: str) -> set[str]:
        return {d["name"] for d in self.dims[metric]}

    def mentioned_values(self, question: str) -> list[tuple[str, str]]:
        """Dimension values named in the question, matched as whole words (state codes in capitals only).
        Review scores and true/false are left out: digits and yes/no words in questions are rarely those values."""
        found = []
        lowered = " " + re.sub(r"[^a-z0-9]+", " ", question.lower().replace("cancelled", "canceled")) + " "
        for name, values in sorted(self.values.items()):
            if name == "order__review_score":
                continue
            for v in values:
                if re.fullmatch(r"[A-Z]{2}", v):
                    hit = re.search(rf"\b{v}\b", question) is not None
                elif v.lower() in ("true", "false"):
                    hit = False
                else:
                    hit = f" {re.sub(r'[^a-z0-9]+', ' ', v.lower()).strip()} " in lowered
                if hit:
                    found.append((name, v))
        # "computers accessories" also contains the value "computers": keep only the longest value named there.
        phrase = {f: re.sub(r"[^a-z0-9]+", " ", f[1].lower()).strip() for f in found}
        return [f for f in found if not any(f != g and f" {phrase[f]} " in f" {phrase[g]} " for g in found)]

    def similar_examples(self, question: str, k: int = TOP_EXAMPLES) -> list[dict]:
        return [self.examples[int(i)] for i, _ in self.example_index.rank(question)[:k]]

    def context(self, question: str) -> Context:
        names = self.select_metrics(question)
        metrics = [m for m in self.catalog if m["name"] in names]
        metrics.sort(key=lambda m: names.index(m["name"]))
        dimensions: dict[str, dict] = {}
        for name in names:
            for d in self.dims[name]:
                entry = dimensions.setdefault(d["name"], {"metrics": []})
                entry["metrics"].append(name)
                values = d.get("values") or []
                if len(values) <= LISTED_VALUES:
                    entry["values"] = values
        return Context(metrics, dimensions, self.mentioned_values(question), self.similar_examples(question))


# ---------- part 3: answer checks ----------

DATA_START, DATA_END = "2016-09-04", "2018-10-17"
RELATIVE = re.compile(r"\b(last|this|previous|past) (year|quarter|month|week)\b|\bso far\b|\byear to date\b|\bytd\b", re.I)
RANKING = re.compile(r"\b(which|top|highest|most|lowest|least|fewest|best|worst|largest|smallest|biggest)\b", re.I)
TOP_N = re.compile(r"\btop (\d+)\b", re.I)


def _successful_queries(trace: list[dict]) -> list[tuple[dict, list]]:
    return [(s["arguments"], s["result"]["rows"]) for s in trace
            if s.get("tool") == "query_metrics" and isinstance(s.get("result"), dict) and "rows" in s["result"]]


def _rows_within(answer, result: list) -> bool:
    if not isinstance(answer, list) or not answer:
        return answer == result
    pool = [json.dumps(r, default=str) for r in result]
    return all(json.dumps(r, default=str) in pool for r in answer)


class AnswerCheck:
    """Checks a final answer against the query results and the question. Each check is a rule anyone can read;
    none of them calls a model. Returns {"final", "feedback", "found"}."""

    def __init__(self, harness: Harness, question: str) -> None:
        self.harness, self.question = harness, question
        self.years = sorted(set(re.findall(r"\b(201[6-8])\b", question)))
        self.mentioned = harness.mentioned_values(question)

    def __call__(self, final: dict, trace: list[dict]) -> dict:
        found: list[str] = []
        queries = _successful_queries(trace)
        if "cannot_answer" in final or not queries:
            return {"final": final, "feedback": None, "found": found}
        args, rows = queries[-1]
        # Copy: every row of the answer must be a row of one query's result (the answer may keep only some of
        # them, e.g. the top row). If not, a number was changed: use the last query's rows, which is what the
        # prompt asks for anyway ("copy numbers from query results").
        if not any(_rows_within(final.get("rows"), r) for _, r in queries):
            final = {**final, "rows": rows,
                     "columns": next((s["result"].get("columns") for s in reversed(trace)
                                      if s.get("tool") == "query_metrics" and isinstance(s.get("result"), dict)
                                      and "rows" in s["result"]), final.get("columns"))}
            found.append("copy")
        problems = [p for p in (self._empty(rows), self._dates(args), self._ranking(args), self._values(args)) if p]
        found += [name for name, _ in problems]
        feedback = None
        if problems:
            feedback = ("Before this answer is shown, the harness checked it and found:\n"
                        + "\n".join(f"- {text}" for _, text in problems)
                        + "\nFix the query if needed and reply with the final JSON. If the answer is right as it is, "
                          "reply with the same JSON.")
        return {"final": final, "feedback": feedback, "found": found}

    def _empty(self, rows: list) -> tuple[str, str] | None:
        if rows:
            return None
        return ("empty", "The query returned no rows. Check filter values against the listed values, and that the dates "
                         f"fall within the data ({DATA_START} to {DATA_END}).")

    def _dates(self, args: dict) -> tuple[str, str] | None:
        start, end = args.get("start"), args.get("end")
        if self.years:
            if not start and not end:
                return ("dates", f"The question names {', '.join(self.years)} but the query has no start or end date.")
            lo, hi = (start or DATA_START)[:4], (end or DATA_END)[:4]
            missing = [y for y in self.years if not lo <= y <= hi]
            if missing:
                return ("dates", f"The question names {', '.join(missing)} but the query's dates are {start} to {end}.")
        elif RELATIVE.search(self.question) and not start and not end:
            return ("dates", f"The question names a relative period (today is {DATA_END}) but the query has no dates.")
        return None

    def _ranking(self, args: dict) -> tuple[str, str] | None:
        if not args.get("group_by") or not RANKING.search(self.question):
            return None
        if not args.get("order_by"):
            return ("ranking", "The question asks for a ranking but the query has no order_by.")
        n = TOP_N.search(self.question)
        if n and args.get("limit") not in (int(n.group(1)), None):
            return ("ranking", f"The question asks for the top {n.group(1)} but the query's limit is {args.get('limit')}.")
        return None

    def _values(self, args: dict) -> tuple[str, str] | None:
        used = {f.get("dimension") for f in args.get("filters") or [] if isinstance(f, dict)} | set(args.get("group_by") or [])
        about = " ".join(m.replace("_", " ") + " " + (self.harness.description(m) or "") for m in args.get("metrics") or []).lower()
        ignored = [(d, v) for d, v in self.mentioned if d not in used and v.lower().replace("_", " ") not in about]
        if ignored:
            return ("values", "The question names " + "; ".join(f"{d} = {v}" for d, v in ignored)
                    + " but the query neither filters nor groups by it.")
        # Grouped by a dimension whose values the question names, without filtering to them: the answer
        # would list every value (all 27 states) instead of the named ones.
        filtered = {f.get("dimension") for f in args.get("filters") or [] if isinstance(f, dict)}
        named = {}
        for d, v in self.mentioned:
            named.setdefault(d, []).append(v)
        loose = [d for d, vs in named.items() if d in (args.get("group_by") or []) and d not in filtered and len(vs) > 1]
        if loose and not args.get("limit"):
            return ("values", f"The question names {', '.join(named[loose[0]])} for {loose[0]}, but the query groups by "
                              "every value without filtering to those.")
        return None


async def harness_ask(question: str, server, harness: Harness, llm, previous: dict | None = None) -> dict:
    """Answer one question with the harness: selected context in, checked answer out."""
    ctx = harness.context(question)
    message = f"{ctx.text()}\n\nQuestion: {question}"
    out = await ask(message, server, llm, HARNESS_SYSTEM, previous=previous, check=AnswerCheck(harness, question))
    out["harness"] = {"metrics": [m["name"] for m in ctx.metrics], "mentioned": ctx.mentioned,
                      "examples": [e["question"] for e in ctx.examples], "context_chars": len(ctx.text())}
    return out
