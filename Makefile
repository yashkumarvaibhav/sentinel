SHELL := /bin/bash
.DEFAULT_GOAL := help

# Every Python gate runs from platform/ so that tool config, the import root and
# the uv environment all resolve to the same place.
PY := cd platform && uv run

# Targets that are planned but not yet built fail loudly rather than pretending
# to succeed — a green stub is worse than a missing one.
define todo
	@echo "make $(1): not implemented yet — lands in Phase $(2)." >&2; exit 1
endef

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-z][a-zA-Z0-9_-]*:.*?## ' $(MAKEFILE_LIST) \
		| awk -F':.*?## ' '{printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

# --- verification -----------------------------------------------------------

.PHONY: verify
verify: verify-py ## Run every lint, typecheck and test gate

.PHONY: verify-py
verify-py: ## Lint, typecheck and test the Python planes
	$(PY) ruff format --check .
	$(PY) ruff check .
	$(PY) mypy .
	$(PY) pytest

.PHONY: fmt
fmt: ## Autoformat the Python planes
	$(PY) ruff format .
	$(PY) ruff check --fix .

.PHONY: install
install: ## Sync the Python environment
	cd platform && uv sync --all-extras

# --- stack ------------------------------------------------------------------

.PHONY: up
up: ## Bring the local stack up (docker compose)
	$(call todo,up,0.4)

.PHONY: down
down: ## Tear the local stack down
	$(call todo,down,0.4)

.PHONY: seed
seed: ## Load config and seed the datastores
	$(call todo,seed,1.2)

# --- evaluation lab ---------------------------------------------------------

.PHONY: lab-up
lab-up: ## Bring up the instrumented testbed (k3s + demo mesh)
	$(call todo,lab-up,0.7)

.PHONY: score
score: ## Score the pipeline on held-out seeds (gate)
	$(call todo,score,1.8)

.PHONY: golden
golden: ## Replay goldens and diff decision transcripts (gate)
	$(call todo,golden,1.8)
