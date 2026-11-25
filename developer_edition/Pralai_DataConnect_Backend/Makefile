.PHONY: backend frontend compose install test lint audit sbom

# ── Tests (GAP-06) ───────────────────────────────────────────────────────────────────────────────────────────────────
# `make test` is the one command that works from a fresh clone: it creates .venv if it is missing, installs the project with its
# dev extras (`pip install -e ".[dev]"`) only when pyproject.toml has changed, then runs pytest. Pass pytest arguments with
# PYTEST_ARGS, e.g.   make test PYTEST_ARGS="-k topic --no-cov"
PYTHON ?= python3
VENV   ?= .venv
VPY    := $(VENV)/bin/python
STAMP  := $(VENV)/.dev-installed

$(VPY):
	$(PYTHON) -m venv $(VENV)

$(STAMP): pyproject.toml | $(VPY)
	$(VPY) -m pip install -e ".[dev]"
	touch $@

install: $(STAMP)

test: install
	$(VPY) -m pytest $(PYTEST_ARGS)

lint: install
	$(VPY) -m ruff check app tests

# ── Supply chain (docs/SUPPLY_CHAIN.md) ────────────────────────────────────────────────────────────────────────────────
# `make audit` fails on any known-vulnerable dependency of THIS environment (private, non-PyPI packages are skipped with a notice).
# `make sbom` writes a CycloneDX bill of materials to sbom/. Both need the `security` extra, installed on demand.
SECURITY_STAMP := $(VENV)/.security-installed

$(SECURITY_STAMP): $(STAMP) pyproject.toml
	$(VPY) -m pip install -e ".[security]"
	touch $@

audit: $(SECURITY_STAMP)
	$(VPY) -m pip_audit --skip-editable --progress-spinner off

sbom: $(SECURITY_STAMP)
	mkdir -p sbom
	$(VPY) -m cyclonedx_py environment $(VPY) --of JSON -o sbom/backend.cdx.json
	@echo "wrote sbom/backend.cdx.json"

backend:
	cd backend && uvicorn app.main:app --reload

frontend:
	cd frontend && npm run dev

compose:
	cd infra && docker compose up --build

