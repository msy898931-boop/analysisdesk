# AnalysisDesk — one entry point for the checks CI runs.
#
# The gates are the same ones .github/workflows/ci.yml runs, so `make check`
# locally means the same thing as a green build. Keeping them in one place stops
# the two from drifting, which is the failure this project keeps removing.

PY ?= .venv/bin/python
NODE ?= node

.PHONY: help install test ui lint types security audit check run docker clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Create .venv and install the app plus dev tools
	uv venv .venv --python 3.11
	uv pip install --python .venv -e ".[dev]"

test:  ## Run the Python suite (network tests deselected, as in CI)
	$(PY) -m pytest tests/ -m "not network" --cov=app --cov-report=term-missing

ui:  ## Run the browser-side tests under node
	$(NODE) --test web/app.test.mjs web/dom.test.mjs

lint:  ## Ruff over the code, tests and scripts
	$(PY) -m ruff check app/ tests/ scripts/

types:  ## mypy over the app package
	$(PY) -m mypy

security:  ## Bandit over the code and scripts
	$(PY) -m bandit -q -r app/ scripts/ -ll

audit:  ## Audit the lock the image installs
	$(PY) -m pip_audit -r requirements.lock

check: lint types security test ui  ## Everything CI runs

run:  ## Serve locally on http://127.0.0.1:12000
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port 12000 --no-proxy-headers

docker:  ## Build the image and prove it becomes healthy as a non-root user
	docker build -t analysisdesk:local .
	docker run -d --name desk-local -p 127.0.0.1:12000:12000 -e PORT=12000 analysisdesk:local
	@for i in $$(seq 1 30); do \
		curl -fsS http://127.0.0.1:12000/api/health && exit 0; \
		sleep 1; \
	done; \
	echo "never became healthy"; docker logs desk-local; exit 1

clean:  ## Stop and remove the local container
	-docker rm -f desk-local
