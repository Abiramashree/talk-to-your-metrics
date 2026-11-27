PY := .venv/bin/python
DBT := DBT_PROFILES_DIR=dbt .venv/bin/dbt

.PHONY: data build answers test serve mcp bench score gate heldout heldout-score

data:
	$(PY) scripts/load_olist.py

build:
	$(DBT) build --project-dir dbt

answers:
	$(PY) bench/freeze.py

test:
	$(PY) -m pytest -q -p no:logging

serve:
	.venv/bin/uvicorn ttym.api:app --port 8000 --no-server-header

mcp:
	$(PY) -m ttym.mcp_server

# make bench SETUP=C SPLIT=dev   (runs only what changed; stops cleanly at the daily limit)
bench:
	$(PY) bench/run.py --setup $(SETUP) --split $(SPLIT)

score:
	$(PY) bench/score.py $(or $(SPLIT),test)

# Held-out set (harness comparison): make heldout SETUP=C   (live model calls; ask before running all 30)
heldout:
	$(PY) bench/run_heldout.py --setup $(SETUP)

heldout-score:
	$(PY) bench/score_heldout.py

# The gate every change passes before merge; no model calls. CI runs it.
gate: test
	$(PY) bench/check_heldout.py
	$(PY) bench/score_heldout.py
