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
[Cube](https://arxiv.org/abs/2604.25149)) show this helps. What they don't separate is how much comes from
the layer itself and how much from modeling it for agents: clear definitions, synonyms, stated
rules. This project measures that, on real data.

## Method

- Correct answers are SQL on the raw tables, never the semantic layer ([questions](bench/questions.yaml), [definitions](bench/definitions.md)).
- Test questions are never run through the layer before scoring; tuning uses the dev questions only.
- [Scoring rules](bench/scoring.md) and [predictions](bench/predictions.md) were committed before any run.

## Status

- [x] Semantic model: dbt + MetricFlow on DuckDB, 18 metrics, 18 data tests
- [x] Benchmark: 30 test questions (24 with correct SQL, 6 the data can't answer) and 8 dev questions
- [x] MCP server and REST API over one core
- [x] Agent page: chat, charts, and a query playground where any answer can be regrouped, filtered or re-sorted without the AI
- [ ] Results: raw text-to-SQL vs semantic layer vs semantic layer modeled for agents
- [ ] Hosted demo

## Run

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
make data    # downloads Olist and loads it into DuckDB
make build   # builds the dbt models and runs the tests
make test    # tests the core, MCP server and REST API
```

## Use

Three tools, the same over MCP and REST: `list_metrics`, `get_dimensions`, `query_metrics`.
Queries name metrics, dimensions and filters; MetricFlow writes the SQL. The database is opened read-only.

Claude Code:

```bash
claude mcp add talk-to-your-metrics -- uv run --directory /path/to/talk-to-your-metrics python -m ttym.mcp_server
```

The page at `/`, REST, and MCP over HTTP at `/mcp` (the page needs `GROQ_API_KEY` or `GEMINI_API_KEY` in `.env`):

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
