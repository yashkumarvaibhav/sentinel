SHELL := /bin/bash
.DEFAULT_GOAL := help

# Every Python gate runs from platform/ so that tool config, the import root and
# the uv environment all resolve to the same place.
PY := cd platform && uv run
WEB := cd web && npm run --silent

# The project directory is pinned to the repo root so that relative paths in the
# compose file and the root .env resolve the same way from anywhere.
# Stamped into the images so /api/version and the web footer name the commit
# that is actually serving.
GIT_SHA := $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
BUILT_AT := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)

COMPOSE := SENTINEL_GIT_SHA=$(GIT_SHA) SENTINEL_BUILT_AT=$(BUILT_AT) \
	docker compose --project-name sentinel --project-directory . -f deploy/docker-compose.yml

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
verify: verify-py verify-web ## Run every lint, typecheck and test gate

.PHONY: verify-py
verify-py: ## Lint, typecheck and test the Python planes
	$(PY) ruff format --check .
	$(PY) ruff check .
	$(PY) mypy .
	$(PY) python -m contracts export --check
	$(PY) python -m common.config --path ../config
	$(PY) pytest

.PHONY: verify-web
verify-web: ## Typecheck, lint, test and build the web app
	$(WEB) contracts:check
	$(WEB) typecheck
	$(WEB) lint
	$(WEB) test
	$(WEB) build

.PHONY: fmt
fmt: ## Autoformat the Python planes
	$(PY) ruff format .
	$(PY) ruff check --fix .

.PHONY: install
install: ## Sync both toolchains
	cd platform && uv sync --all-extras
	cd web && npm install

.PHONY: contracts
contracts: ## Export JSON Schema and regenerate TypeScript contracts
	$(PY) python -m contracts export
	$(WEB) contracts:generate

.PHONY: config-check
config-check: ## Validate all versioned operator configuration
	$(PY) python -m common.config --path ../config

.PHONY: migrate
migrate: ## Apply idempotent ClickHouse and Postgres migrations
	$(COMPOSE) run --rm --build gateway python -m common.storage migrate

.PHONY: verify-storage
verify-storage: ## Run real repository round trips on the compose datastores
	$(COMPOSE) run --rm --build -e SENTINEL_STORAGE_INTEGRATION=1 gateway \
		env UV_CACHE_DIR=/tmp/sentinel-uv-cache \
		uv run --extra api --extra storage pytest tests/test_storage_integration.py

# --- stack ------------------------------------------------------------------

.PHONY: up
up: ## Bring the local stack up and wait for it to be healthy
	$(COMPOSE) up -d --build --wait

.PHONY: down
down: ## Stop the local stack (volumes are kept)
	$(COMPOSE) down

.PHONY: nuke
nuke: ## Stop the local stack and delete its data volumes
	$(COMPOSE) down -v

.PHONY: ps
ps: ## Show stack status
	$(COMPOSE) ps

.PHONY: logs
logs: ## Follow stack logs (SVC=name to narrow)
	$(COMPOSE) logs -f --tail=100 $(SVC)

.PHONY: seed
seed: ## Load config and seed the datastores
	$(call todo,seed,1.2)

# --- evaluation lab ---------------------------------------------------------

.PHONY: lab-up
lab-up: ## Bring up the testbed cluster (k3s in docker, on sentinel_net)
	./lab/testbed/lab-up.sh

.PHONY: lab-down
lab-down: ## Stop the testbed cluster, keeping it for the next run
	./lab/testbed/lab-down.sh stop

.PHONY: lab-destroy
lab-destroy: ## Delete the testbed cluster entirely
	./lab/testbed/lab-down.sh delete

.PHONY: lab-deploy
lab-deploy: ## Deploy the instrumented mesh into the testbed
	./lab/testbed/lab-deploy.sh

.PHONY: lab-undeploy
lab-undeploy: ## Remove the instrumented mesh, keeping the cluster
	helm --kube-context k3d-sentinel-lab -n otel-demo uninstall astronomy

.PHONY: lab-chaos
lab-chaos: ## Run a chaos experiment (EXPERIMENT=name)
	./lab/testbed/lab-chaos.sh

.PHONY: lab-load
lab-load: ## Run a load profile against the testbed (PROFILE=, RATE=, DURATION=)
	./lab/loadgen/lab-load.sh

.PHONY: lab-status
lab-status: ## Show testbed nodes and workloads
	kubectl --context k3d-sentinel-lab get nodes -o wide
	kubectl --context k3d-sentinel-lab get pods -A

.PHONY: score
score: ## Score the pipeline on held-out seeds (gate)
	$(call todo,score,1.8)

.PHONY: golden
golden: ## Replay goldens and diff decision transcripts (gate)
	$(call todo,golden,1.8)
