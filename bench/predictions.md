# Predictions

Written before any benchmark run, so the results can be checked against them.

## Setup A (raw text-to-SQL) vs the semantic layer

Expected to fail more often on the traps the semantic layer settles:

| Trap | Questions | Likely mistake |
|---|---|---|
| revenue-definition | t01, t02, t04, t06, t22–t24 | counting every status, using payments, or adding freight |
| customer-identity | t07, t08, t09 | counting `customer_id`, which is new on every order (t07 would return 0) |
| order-status | t13 | average order value over all orders, or including freight |
| late-definition | t16, t17 | comparing timestamps, which counts on-the-day deliveries as late |
| duplicate-reviews | t19, t20, t21 | averaging every review instead of one per order |

Expected: A gets clearly fewer answerable questions right than either semantic setup.

## Setup B (bare) vs setup C (modeled for agents)

The metric names already carry most of the meaning, so most questions should come out the same.
Questions where C's descriptions, synonyms and example values could change the outcome:

| Question | Why |
|---|---|
| t09 | "customers who placed an order" could map to `new_customers` without descriptions |
| t21 | filtering on review score without knowing its values |
| t24 | filtering on payment type without knowing the value is spelled `boleto` |
| u05 | "return rate" may be answered with `cancellation_rate` in either setup; C's description says it counts canceled orders, which may make the mismatch clearer |

Expected: B and C differ on 0–4 of 30 questions. With 30 questions that gap is within noise,
so results are reported per trap. If the gap is zero, that is the finding: once metric names are
good, descriptions added little for this model on this data.
