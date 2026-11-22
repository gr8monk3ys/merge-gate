PY ?= .venv/bin/python

.PHONY: check lint test validate

check: lint test validate

lint:
	$(PY) -m ruff check .

test:
	$(PY) -m pytest -q

validate:
	$(PY) classify_pr.py --validate
