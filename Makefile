PY ?= python3

.PHONY: run once dry-run status test

run:
	$(PY) -m lightkeeper

once:
	$(PY) -m lightkeeper --once

dry-run:
	$(PY) -m lightkeeper --dry-run

status:
	$(PY) -m lightkeeper --status

test:
	$(PY) -m unittest discover -s tests
