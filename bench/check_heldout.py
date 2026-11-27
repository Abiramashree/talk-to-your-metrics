"""Check every held-out question before any model sees it. No LLM calls.

    python bench/check_heldout.py

Answerable questions: the correct SQL on the raw tables and the listed semantic-layer query must give the
same rows (scored with the benchmark's matching rules). Questions that should be declined: the listed query
must be refused by the semantic layer. Exits 1 if any question fails.
"""

from __future__ import annotations

import json
import pathlib
import sys

import yaml

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

from check_app import expected  # noqa: E402
from score import rows_match  # noqa: E402
from ttym.core import Filter, Query, QueryError, SemanticLayer  # noqa: E402


def to_query(spec: dict) -> Query:
    return Query(metrics=spec["metrics"], group_by=spec.get("group_by", []),
                 filters=[Filter(f["dimension"], f["operator"], f["value"]) for f in spec.get("filters", [])],
                 start=spec.get("start"), end=spec.get("end"), order_by=spec.get("order_by", []),
                 limit=spec.get("limit"))


def main() -> int:
    questions = yaml.safe_load((HERE / "heldout.yaml").read_text())
    ids = [q["id"] for q in questions]
    assert len(ids) == len(set(ids)), "duplicate ids"
    # As the answers are stored and compared everywhere else: through JSON (decimals as numbers, dates as text).
    gold = json.loads(json.dumps(expected(questions), default=lambda v: float(v) if hasattr(v, "as_tuple") else str(v)))
    layer = SemanticLayer()
    failed = 0
    for q in questions:
        if q["trap"] == "unanswerable":
            if "layer_fails" not in q:
                print(f"{q['id']}  ok     decline ({q['why']})")
                continue
            try:
                layer.query(to_query(q["layer_fails"]))
                ok, reason = False, "the layer answered a query it should refuse"
            except QueryError as e:
                ok, reason = True, f"refused: {str(e)[:90]}"
        else:
            rows = layer.query(to_query(q["layer"]))["rows"]
            ok, reason = rows_match(gold[q["id"]], q["kinds"], rows, top_row_only=q.get("top_row", False))
            if not gold[q["id"]]:
                ok, reason = False, "the correct SQL returned no rows"
        failed += not ok
        print(f"{q['id']}  {'ok   ' if ok else 'FAIL '}  {reason}")
    print(f"{len(questions) - failed}/{len(questions)} checked")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
