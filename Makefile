PY := .venv/bin/python
DBT := DBT_PROFILES_DIR=dbt .venv/bin/dbt

.PHONY: data build answers

data:
	$(PY) scripts/load_olist.py

build:
	$(DBT) build --project-dir dbt

answers:
	$(PY) bench/freeze.py
