SHELL := /bin/bash
.DEFAULT_GOAL := help

# Every Python gate runs from platform/ so that tool config, the import root and
# the uv environment all resolve to the same place.
PY := cd platform && PYTHONPATH=.. uv run
PY_PATHS := . ../lab
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
	$(PY) ruff format --check $(PY_PATHS)
	$(PY) ruff check $(PY_PATHS)
	$(PY) mypy $(PY_PATHS)
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
	$(PY) ruff format $(PY_PATHS)
	$(PY) ruff check --fix $(PY_PATHS)

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

.PHONY: verify-ingest
verify-ingest: ## Run the real raw bus to normalized bus/ClickHouse round trip
	$(COMPOSE) run --rm --build -e SENTINEL_LAB_INGEST_INTEGRATION=1 ingest \
		env UV_CACHE_DIR=/tmp/sentinel-uv-cache \
		uv run --extra ingest --extra storage pytest tests/test_ingest_integration.py

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

.PHONY: data-pull
data-pull: ## Fetch and verify the DVC-versioned Phase 1 capture store
	$(PY) python -m lab.captures.bootstrap \
		--repo-root .. --manifest ../lab/captures/bootstrap.json
	$(PY) dvc pull ../data/captures/phase-1.dvc -r bootstrap

.PHONY: score
score: data-pull ## Score held-out capture replays (hosted gate)
	$(PY) python -m lab.scoring.capture \
		--repo-root .. --captures-root ../data/captures/phase-1/held-out \
		--report ../docs/reports/phase-1-decomposition-score.md

.PHONY: score-live
score-live: ## Run fresh held-out seeds against the contained live testbed
	cd platform && PYTHONPATH=.. uv run python -m lab.scoring \
		--repo-root .. --report ../docs/reports/phase-1-decomposition-score.md

.PHONY: capture
capture: ## Record one committed profile seed (PROFILE= SEED= CAPTURE_ID= [PURPOSE=held_out])
	@test -n "$(PROFILE)" -a -n "$(SEED)" -a -n "$(CAPTURE_ID)" || \
		{ echo "PROFILE, SEED and CAPTURE_ID are required" >&2; exit 2; }
	cd platform && PYTHONPATH=.. uv run python -m lab.captures record \
		--repo-root .. --profile "$(PROFILE)" \
		--seed "$(SEED)" --purpose "$(or $(PURPOSE),held_out)" \
		--capture-id "$(CAPTURE_ID)" \
		--output "../var/captures/$(CAPTURE_ID)"

.PHONY: replay-capture
replay-capture: ## Replay one raw capture through decomposition (CAPTURE_ID=)
	@test -n "$(CAPTURE_ID)" || { echo "CAPTURE_ID is required" >&2; exit 2; }
	cd platform && PYTHONPATH=.. uv run python -m lab.captures replay \
		--repo-root .. --capture "../var/captures/$(CAPTURE_ID)" \
		--transcript "../var/replays/$(CAPTURE_ID)/decomposition.json"

.PHONY: score-captures
score-captures: ## Gate an exact four-seed capture matrix (CAPTURE_ROOT=)
	@test -n "$(CAPTURE_ROOT)" || { echo "CAPTURE_ROOT is required" >&2; exit 2; }
	cd platform && PYTHONPATH=.. uv run python -m lab.scoring.capture \
		--repo-root .. --captures-root "../$(CAPTURE_ROOT)" \
		--report ../var/reports/phase-1-capture-score.md

.PHONY: golden
golden: data-pull ## Replay development goldens and diff semantic transcripts
	cd platform && PYTHONPATH=.. uv run python -m lab.scoring.golden \
		--repo-root .. \
		--captures-root "../$(or $(GOLDEN_CAPTURE_ROOT),data/captures/phase-1/goldens)" \
		--goldens-root ../lab/goldens/phase-1

.PHONY: regen-golden
regen-golden: data-pull ## Rewrite reviewed Phase 1 goldens from development captures
	cd platform && PYTHONPATH=.. uv run python -m lab.scoring.golden \
		--repo-root .. \
		--captures-root "../$(or $(GOLDEN_CAPTURE_ROOT),data/captures/phase-1/goldens)" \
		--goldens-root ../lab/goldens/phase-1 --write
