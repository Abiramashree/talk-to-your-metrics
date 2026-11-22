# Scoring rules

Written before any benchmark run. Every setup is scored by the same rules.

## Setups

All three use the same model (Groq `openai/gpt-oss-120b`, temperature 0), the same agent loop
(`src/ttym/agent.py`: a tool call is required on the first turn; if the provider rejects a tool call
as invalid, that turn is retried once with no tools offered, so the model replies in text; at most
8 tool calls), the same
final-answer format below, and one run per question. Only the tools and what they return differ.

| Setup | Tools | What the model sees |
|---|---|---|
| A, raw text-to-SQL | `get_schema`, `run_sql` | A read-only copy of the 9 raw tables only: column names and types, row counts, and the values of text columns with at most 10 distinct values. Queries return at most 200 rows and stop after 10 seconds. |
| A+, raw text-to-SQL with definitions | same as A | A, plus `bench/definitions.md` (from "Time" on) appended to the system prompt. Added after the first test run. |
| B, semantic layer, bare | `list_metrics`, `get_dimensions`, `query_metrics` | Metric and dimension names only. No descriptions, synonyms or example values. |
| C, semantic layer, modeled for agents | same as B | B plus descriptions, synonyms, example dimension values, and each metric's definition returned with its result. |

B and C run against the same metrics, joins and SQL; only the descriptive metadata differs.

## The answer

Each run ends with one final answer:

- `{"columns": [...], "rows": [[...]]}` copied from a query result, or
- `{"cannot_answer": "<reason>"}`.

No final answer, an error, a timeout or more than 8 tool calls counts as wrong.

Added after the first test run, for every later run: a provider error other than a used-up daily limit
is saved as a failed run with its tool calls and is not retried. Every run also writes a log
(`bench/results/logs/`).

## Answerable questions

An answer is correct when every expected row is matched and no unexpected row remains.

- **Rows** are compared as a set; order does not matter.
- **Key values** (text, dates, true/false) identify a row. Text is compared trimmed and
  case-insensitively. Dates are compared on `YYYY-MM-DD`; a year key matches a date in that year.
- **Numeric values** are matched by kind (per question in `scoring.yaml`), in any column:

  | Kind | Match when |
  |---|---|
  | count | exactly equal |
  | money, average | within 0.005 |
  | rate | within 0.00005 as a fraction, or within 0.005 as a percentage |

- **Extra rows whose key is empty** are ignored (for example, orders that were never delivered
  when grouping by late / on time). Any other extra row makes the answer wrong.
- **"Which …" questions** (t03, t10) are correct when the top row's key matches; a value, if
  given, must also match.

## Unanswerable questions

Mechanical rule, no judgment:

- An unanswerable question is correct when the final answer is `{"cannot_answer": ...}` and has no
  `rows`. The text of the reason is not scored, even if it mentions other figures (for example
  "returns aren't recorded; the cancellation rate was 0.6%" is correct).
- Any final answer with `rows` is wrong for an unanswerable question, whatever the rows contain.
- An answerable question answered with `cannot_answer` is wrong.

## Reporting

- Accuracy per setup over all 30 test questions, with a Wilson 95% interval.
- Accuracy per trap (see `trap` in `questions.yaml`), so a difference is visible where it happens.
- Every raw answer and every tool call is published next to the score.
- **Copy fidelity**, per setup: for each answered question, whether the rows in the final answer
  match the rows of the last successful query result (setups B and C) or of the last successfully
  executed SQL (setup A), under the same matching rules as above. Reported as a count of answers
  where they differ, and how many of those were scored wrong because of it. This separates
  "the data was right but the model copied it wrong" from "the query itself was wrong".
- Repeated runs at temperature 0 are not independent; intervals are reported per question set, not per run.
