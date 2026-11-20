# talk-to-your-metrics

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
its own SQL. Vendor benchmarks show this helps. What they don't separate is how much comes from
the layer itself and how much from modeling it for agents: clear definitions, synonyms, stated
rules. This project measures that, on real data, with questions frozen before the model was written.

## Status

- [x] Semantic model: dbt + MetricFlow on DuckDB, 18 metrics, 30 data tests
- [x] Benchmark: 30 test and 8 dev questions with correct SQL, committed before the model
- [ ] MCP server and REST API over one core
- [ ] Agent page where every answer shows its definition, filters and SQL
- [ ] Results: raw text-to-SQL vs semantic layer vs semantic layer modeled for agents
- [ ] Hosted demo

## Run

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
make data    # downloads Olist and loads it into DuckDB
make build   # builds the dbt models and runs the tests
```

## Data

[Brazilian E-Commerce Public Dataset by Olist](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce),
CC BY-NC-SA 4.0. Downloaded by `make data`, never stored in this repository.

## License

Code: MIT.
