# talk-to-your-metrics

[![ci](https://github.com/Abiramashree/talk-to-your-metrics/actions/workflows/ci.yml/badge.svg)](https://github.com/Abiramashree/talk-to-your-metrics/actions/workflows/ci.yml)

Ask an AI agent a business question and it writes SQL and returns a number. On real data the
number is often wrong, not because the SQL fails, but because the question has more than one
reasonable answer and the agent quietly picks one.

On the Olist e-commerce data (99,441 orders, 2016–2018):

| Question | Answers a reasonable query can return |
|---|---|
| What was total revenue? | 13.22M, 13.59M, 15.84M or 16.01M, depending on freight, payments and canceled orders |
| How many customers ordered more than once? | 0 or 2,997. Olist issues a new `customer_id` on every order |
| What share of deliveries were late? | 8.1% or 6.8%. Comparing timestamps counts on-the-day deliveries as late |

A semantic layer defines each metric once, so an agent asks for `revenue` instead of writing
its own SQL. Vendor benchmarks ([dbt](https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026),
[Cube](https://arxiv.org/abs/2604.25149)) show this helps, but don't separate how much comes from the
layer itself, from knowing the business rules, or from modeling the layer for agents. This project
connects a semantic layer to an agent through MCP and a REST API, and measures each of those.

## Results

Same agent, same model (Groq `gpt-oss-120b`, temperature 0), same 30 questions frozen before the
semantic model was written, one run each. Only what the agent is connected to changes.

| The agent is connected to | Correct | Tokens per question |
|---|---|---|
| Raw tables; it writes its own SQL | 10 / 30 | ~2,300 |
| Raw tables, with the business definitions in its prompt | 25 / 30 | ~3,400 |
| The semantic layer, metric names only | 26 / 30 | ~3,300 |
| The semantic layer modeled for agents (descriptions, synonyms, example values) | 30 / 30 | ~5,400 |

**Connected to the semantic layer, the same agent goes from 10 to 26 correct** (exact paired test,
p = 0.0001). Writing its own SQL, it guessed what "revenue" and "customer" mean. Asked how many
customers ordered more than once, it answered 0; the answer is 2,997.

**The gap is the business rules.** Given the definitions in its prompt, the raw-SQL agent reaches 25
(p = 0.00006), level with the names-only layer, but still breaks rules it was told: payments on every
order instead of delivered ones, timestamps instead of dates, every review instead of the latest.
The semantic layer applies them for it, without a rules document in every prompt.

**Descriptions for agents scored 30, but at 30 questions that is not shown** (p = 0.13). The four
questions the names-only layer missed were three tool calls the provider rejected and one misread
"last year"; none was clearly caused by the missing descriptions, and the descriptions add about 65%
more tokens.

### Why answers were wrong

| Cause | Raw SQL | + definitions | Names only | Modeled |
|---|---|---|---|---|
| Wrong business definition | 16 | 5 | 0 | 0 |
| Misread the question | 1 | 0 | 1 | 0 |
| Tool call rejected by the provider | 3 | 0 | 3 | 0 |
| **Wrong** | **20** | **5** | **4** | **0** |

All six unanswerable questions were declined by every setup that returned an answer.

### Limitations

- One model, one dataset, 30 questions written for this study, one run each.
- The questions were written to probe known ambiguities in this data, so accuracy here is a comparison
  between setups, not an estimate for questions in general. The semantic layer and the correct answers
  follow the same written definitions, as a business's metrics would; the correct answers are computed
  independently, from the raw tables.
- Tokens are prompt plus completion, averaged over the runs the provider answered.
- Setup "raw SQL + definitions" was added after the first test run; its prediction (16–24) was
  committed before it ran, and it scored 25.
- The paired test was chosen after the runs; the scoring rules committed beforehand report Wilson
  intervals ([scores](bench/results/summary-test.json)).
- After that run the scorer was fixed to accept a month written `YYYY-MM`; re-scoring every saved run
  changed one answer.
- Six runs ended in a tool call the provider rejected twice; their traces weren't captured and they
  were not rerun.

The benchmark ran on the 18 metrics at commit `9625327`. The app has since added two metrics for
questions across levels of detail (orders by product category, orders by payment type), explanations when
a metric can't be split by a dimension, and fixes for rejected tool calls; those are checked separately,
on [8 new questions](bench/app_checks.yaml), not counted in the results above.

[Every run, with its tool calls](bench/results/runs-test) · [scoring rules](bench/scoring.md) ·
[predictions](bench/predictions.md)

## Method

- Correct answers are SQL on the raw tables, never the semantic layer ([questions](bench/questions.yaml), [definitions](bench/definitions.md)).
- Test questions are never run through the layer before scoring; tuning uses the dev questions only.
- [Scoring rules](bench/scoring.md) and [predictions](bench/predictions.md) were committed before each run.

## What's here

| Path | |
|---|---|
| `dbt/` | Staging and mart models, 20 MetricFlow metrics, data tests, a reconciliation against the raw tables |
| `src/ttym/core.py` | The one query core: validated metrics, dimensions and filters to MetricFlow |
| `src/ttym/mcp_server.py`, `api.py` | MCP server and REST API over that core |
| `src/ttym/agent.py` | The agent loop, which reaches data only through MCP tools |
| `src/ttym/sql_server.py` | The raw-SQL baseline: read-only raw tables, no semantic layer |
| `bench/` | Questions, correct SQL, scoring rules, predictions, runner, scorer, every saved run |
| `web/` | The agent page |

## Status

- [x] Semantic model: dbt + MetricFlow on DuckDB, 20 metrics, 18 data tests
- [x] Benchmark: 30 test questions (24 with correct SQL, 6 the data can't answer) and 8 dev questions
- [x] MCP server and REST API over one core
- [x] Agent page: chat, charts, and a query playground where any answer can be regrouped, filtered or re-sorted without the AI
- [x] Results
- [ ] Hosted demo

## Run

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
make data    # downloads Olist and loads it into DuckDB
make build   # builds the dbt models and runs the tests
make test    # tests the core, MCP server, REST API, agent and scorer
make score   # re-scores the saved runs; no model calls
```

## Use

Three tools, the same over MCP and REST: `list_metrics`, `get_dimensions`, `query_metrics`.
Queries name metrics, dimensions and filters; MetricFlow writes the SQL. The database is opened read-only.

Claude Code:

```bash
claude mcp add talk-to-your-metrics -- uv run --directory /path/to/talk-to-your-metrics python -m ttym.mcp_server
```

The page at `/`, REST, and MCP over HTTP at `/mcp` (asking questions needs `GROQ_API_KEY` in `.env`; the query panel works without it):

```bash
make serve   # http://localhost:8000
curl -s localhost:8000/query -H 'content-type: application/json' \
  -d '{"metrics": ["revenue"], "group_by": ["metric_time__year"]}'
```

## Data

[Brazilian E-Commerce Public Dataset by Olist](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce),
CC BY-NC-SA 4.0. Downloaded by `make data`, never stored in this repository.

## License

Code: MIT.
