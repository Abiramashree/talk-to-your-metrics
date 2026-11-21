PY := .venv/bin/python
DBT := DBT_PROFILES_DIR=dbt .venv/bin/dbt

.PHONY: data build answers test serve mcp bench score

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
