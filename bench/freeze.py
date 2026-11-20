"""Validate questions.yaml and write the expected answers to answers.json.

Run after `make data`. Reads the raw tables only, on a read-only connection,
so the expected answers never depend on the semantic model being tested.
"""

import datetime
import decimal
import json
import pathlib
import sys
from collections import Counter

import duckdb
import yaml

HERE = pathlib.Path(__file__).resolve().parent
DB = HERE.parent / "data" / "olist.duckdb"
EXPECTED_SPLITS = {("test", True): 24, ("test", False): 6, ("dev", True): 6, ("dev", False): 2}


def check(questions: list[dict]) -> None:
    ids = Counter(q["id"] for q in questions)
    if dupes := [i for i, n in ids.items() if n > 1]:
        sys.exit(f"duplicate ids: {dupes}")
    counts = Counter((q["split"], "sql" in q) for q in questions)
    if counts != Counter(EXPECTED_SPLITS):
        sys.exit(f"split counts {dict(counts)} != {EXPECTED_SPLITS}")
    for q in questions:
        if ("sql" in q) == ("reason" in q):
            sys.exit(f"{q['id']}: needs exactly one of sql / reason")


def plain(value):
    if isinstance(value, (float, decimal.Decimal)):
        return round(float(value), 6)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def main() -> None:
    questions = yaml.safe_load((HERE / "questions.yaml").read_text())
    check(questions)
    con = duckdb.connect(str(DB), read_only=True)
    answers = {}
    for q in questions:
        if "sql" not in q:
            continue
        result = con.execute(q["sql"])
        columns = [d[0] for d in result.description]
        rows = [[plain(v) for v in row] for row in result.fetchall()]
        if not rows or any(v is None for row in rows for v in row):
            sys.exit(f"{q['id']}: empty or null answer {rows}")
        answers[q["id"]] = {"columns": columns, "rows": rows}
    (HERE / "answers.json").write_text(json.dumps(answers, indent=2, ensure_ascii=False) + "\n")
    for qid, a in answers.items():
        shown = a["rows"][0] if len(a["rows"]) == 1 else f"{len(a['rows'])} rows"
        print(f"{qid}  {a['columns']}  {shown}")


if __name__ == "__main__":
    main()
