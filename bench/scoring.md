# Scoring rules

Written before any benchmark run. Every setup is scored by the same rules.

## The answer

Each run ends with one final answer:

- `{"columns": [...], "rows": [[...]]}` copied from a query result, or
- `{"cannot_answer": "<reason>"}`.

No final answer, an error, a timeout or more than 8 tool calls counts as wrong.

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

Correct only when the answer is `cannot_answer` and contains no number presented as the answer.
Answering an answerable question with `cannot_answer` is wrong.

## Reporting

- Accuracy per setup over all 30 test questions, with a Wilson 95% interval.
- Accuracy per trap (see `trap` in `questions.yaml`), so a difference is visible where it happens.
- Every raw answer and every tool call is published next to the score.
- Repeated runs at temperature 0 are not independent; intervals are reported per question set, not per run.
