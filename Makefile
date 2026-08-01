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
	$(PY) python -m ml.config --path ../config/ml-training.yml --envelopes ../config/ml-envelopes.yml --forecast ../config/ml-forecast.yml --anomaly ../config/ml-anomaly.yml --autoencoder ../config/ml-autoencoder.yml --calibration ../config/ml-calibration.yml --drift ../config/ml-drift.yml
	$(PY) python -m decision --agents ../config/decision-agents.yml --deployments ../config/deployments.yml --verdict-rules ../config/verdict-rules.yml --incidents ../config/incidents.yml --policy ../config/policies/action-policy.yml
	$(PY) python -m action --config ../config/action.yml --ladders ../config/ladders.yml
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
	$(PY) python -m ml.config --path ../config/ml-training.yml --envelopes ../config/ml-envelopes.yml --forecast ../config/ml-forecast.yml --anomaly ../config/ml-anomaly.yml --autoencoder ../config/ml-autoencoder.yml --calibration ../config/ml-calibration.yml --drift ../config/ml-drift.yml
	$(PY) python -m decision --agents ../config/decision-agents.yml --deployments ../config/deployments.yml --verdict-rules ../config/verdict-rules.yml --incidents ../config/incidents.yml --policy ../config/policies/action-policy.yml
	$(PY) python -m action --config ../config/action.yml --ladders ../config/ladders.yml

.PHONY: migrate
migrate: ## Apply idempotent ClickHouse and Postgres migrations
	$(COMPOSE) run --rm --build gateway python -m common.storage migrate

.PHONY: verify-storage
verify-storage: ## Run real repository round trips on the compose datastores
	$(COMPOSE) run --rm --build -e SENTINEL_STORAGE_INTEGRATION=1 gateway \
		env UV_CACHE_DIR=/tmp/sentinel-uv-cache \
		uv run --extra api --extra storage pytest tests/test_storage_integration.py

.PHONY: verify-action
verify-action: ## Run the Kubernetes actuator against the live testbed (read-only, dry-run)
	$(PY) env SENTINEL_ACTION_K8S_INTEGRATION=1 \
		pytest tests/test_action_kubernetes.py -k "real_testbed or stays_in_dry_run"

.PHONY: verify-mesh
verify-mesh: ## Run the mesh actuator against a real Envoy (needs `make lab-edge`)
	$(PY) env SENTINEL_ACTION_MESH_INTEGRATION=1 \
		pytest tests/test_action_mesh.py -k "real_envoy or dry_run"

.PHONY: verify-e2e
verify-e2e: ## Drive the north-star flow through a real browser (needs `make up`)
	$(WEB) e2e

.PHONY: verify-canary
verify-canary: ## Widen and unwind a real Envoy through the durable worker (needs `make lab-edge`)
	$(PY) env SENTINEL_ACTION_MESH_INTEGRATION=1 \
		pytest tests/test_action_canary_integration.py

.PHONY: verify-flags
verify-flags: ## Run the flag actuator against a real flagd (needs `make lab-flags`)
	$(PY) env SENTINEL_ACTION_FLAGS_INTEGRATION=1 \
		pytest tests/test_action_flags.py -k "real_flagd or dry_run"

.PHONY: verify-ingest
verify-ingest: ## Run the real raw bus to normalized bus/ClickHouse round trip
	$(COMPOSE) run --rm --build -e SENTINEL_LAB_INGEST_INTEGRATION=1 ingest \
		env UV_CACHE_DIR=/tmp/sentinel-uv-cache \
		uv run --extra ingest --extra storage pytest tests/test_ingest_integration.py

# --- stack ------------------------------------------------------------------

.PHONY: up
up: migrate ## Apply migrations, then bring the local stack up and wait for health
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

.PHONY: lab-edge
lab-edge: ## Run the owned edge proxy alone, with no cluster (ACTION=up|down|url)
	./lab/testbed/envoy-sandbox.sh $(or $(ACTION),up)

.PHONY: lab-flags
lab-flags: ## Run the testbed's flag provider alone, with no cluster (ACTION=up|down|url)
	./lab/testbed/flagd-sandbox.sh $(or $(ACTION),up)

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

.PHONY: train-data
train-data: ## Build the versioned training dataset (synthetic history + dev baselines)
	$(PY) python -m ml.data \
		--repo-root .. --captures-root ../var/captures \
		--out ../data/training/dataset

.PHONY: train
train: ## Train the context-conditioned quantile envelopes to a local bundle
	$(PY) python -m ml.train \
		--repo-root .. --captures-root ../var/captures \
		--out ../var/models/envelopes

.PHONY: forecast
forecast: ## Fit the seasonal STL forecast baselines to a local bundle
	$(PY) python -m ml.forecast_train \
		--repo-root .. --captures-root ../var/captures \
		--out ../var/models/forecast

.PHONY: anomaly
anomaly: ## Fit the multivariate isolation-forest anomaly model to a local bundle
	$(PY) python -m ml.anomaly_train \
		--repo-root .. --out ../var/models/anomaly

.PHONY: autoencoder
autoencoder: ## Train the LSTM auth-sequence autoencoder to a local bundle
	$(PY) python -m ml.autoencoder_train \
		--repo-root .. --out ../var/models/autoencoder

.PHONY: calibrate
calibrate: ## Conformally calibrate the anomaly detector and report coverage
	$(PY) python -m ml.calibrate_train \
		--repo-root .. --out ../var/models/calibration/anomaly.json

.PHONY: drift
drift: ## Run the injected-drift demonstration and report detection
	$(PY) python -m ml.drift_demo --repo-root ..

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

.PHONY: score-held-out-symptoms
score-held-out-symptoms: ## Close Phase 2: score the sealed held-out cascade/combo captures (CAPTURE_ROOT=)
	@test -n "$(CAPTURE_ROOT)" || { echo "CAPTURE_ROOT is required" >&2; exit 2; }
	cd platform && PYTHONPATH=.. uv run python -m lab.scoring.capture \
		--repo-root .. --held-out-symptom-captures-root "../$(CAPTURE_ROOT)" \
		--report ../docs/reports/phase-2-held-out-score.md \
		--proof ../docs/reports/latest-score-proof.json

.PHONY: diagnose-episodes
diagnose-episodes: ## Dump the label-free episode stream for captures (CAPTURE_ROOTS= space-separated)
	@test -n "$(CAPTURE_ROOTS)" || { echo "CAPTURE_ROOTS is required" >&2; exit 2; }
	cd platform && PYTHONPATH=.. uv run python -m lab.scoring.diagnose \
		--repo-root .. \
		$(foreach root,$(CAPTURE_ROOTS),--capture "../$(root)") \
		--report ../docs/reports/phase-2-episode-characterization.md

.PHONY: decide-replay
decide-replay: ## Replay captures through the whole decision plane (CAPTURE_ROOTS= space-separated)
	@test -n "$(CAPTURE_ROOTS)" || { echo "CAPTURE_ROOTS is required" >&2; exit 2; }
	cd platform && PYTHONPATH=.. uv run python -m lab.scoring.decisions \
		--repo-root .. \
		$(foreach root,$(CAPTURE_ROOTS),--capture "../$(root)") \
		$(if $(TRANSCRIPT_DIR),--transcript-dir "../$(TRANSCRIPT_DIR)") \
		--report ../docs/reports/phase-4-decision-characterization.md

.PHONY: remediate-replay
remediate-replay: ## Run recorded decisions through the remediation loop (CAPTURE_ROOTS= space-separated)
	@test -n "$(CAPTURE_ROOTS)" || { echo "CAPTURE_ROOTS is required" >&2; exit 2; }
	cd platform && PYTHONPATH=.. uv run python -m lab.scoring.remediate \
		--repo-root .. \
		$(foreach root,$(CAPTURE_ROOTS),--capture "../$(root)") \
		--report ../docs/reports/phase-5-remediation-replay.md

.PHONY: score-decisions
score-decisions: ## Score decisions against the scenario answer key (CAPTURE_ROOTS= space-separated)
	@test -n "$(CAPTURE_ROOTS)" || { echo "CAPTURE_ROOTS is required" >&2; exit 2; }
	cd platform && PYTHONPATH=.. uv run python -m lab.scoring.decision_gate \
		--repo-root .. \
		$(foreach root,$(CAPTURE_ROOTS),--capture "../$(root)") \
		--report ../$(or $(DECISION_REPORT),docs/reports/phase-4-decision-score.md)

.PHONY: demo-runner
demo-runner: ## Claim and execute queued /demo scenario runs (repo mounted; Ctrl-C to stop)
	$(COMPOSE) --profile demo run --rm --build --no-deps \
		--user "$(shell id -u):$(shell id -g)" demo-runner

.PHONY: score-live-run
score-live-run: ## Score what the live producer STORED during one authored run (CAPTURE_ID=)
	@test -n "$(CAPTURE_ID)" || { echo "CAPTURE_ID is required" >&2; exit 2; }
	$(COMPOSE) run --rm --no-deps -T \
		--user "$(shell id -u):$(shell id -g)" \
		--volume "$(CURDIR)/lab:/app/lab:ro" \
		--volume "$(CURDIR)/var:/sentinel-var" \
		--volume "$(CURDIR)/docs:/sentinel-docs" \
		--entrypoint "" producer \
		env PYTHONPATH=/app HOME=/tmp \
		python -m lab.scoring.live_gate \
		--repo-root /app --capture "/sentinel-var/captures/$(CAPTURE_ID)" \
		$(if $(SPEND_HELD_OUT_SEED),--spend-held-out-seed) \
		--report "/sentinel-docs/reports/$(or $(LIVE_RUN_REPORT),phase-6-live-run-score.md)"

.PHONY: score-negative-control
score-negative-control: ## Assert no-fault captures emit zero fault-kind episodes (CAPTURE_ROOTS=)
	@test -n "$(CAPTURE_ROOTS)" || { echo "CAPTURE_ROOTS is required" >&2; exit 2; }
	cd platform && PYTHONPATH=.. uv run python -m lab.scoring.diagnose \
		--repo-root .. \
		$(foreach root,$(CAPTURE_ROOTS),--capture "../$(root)") \
		--assert-no-fault-episodes \
		--report ../docs/reports/phase-2-negative-control.md

.PHONY: golden
golden: data-pull ## Replay development goldens and diff semantic transcripts
	cd platform && PYTHONPATH=.. uv run python -m lab.scoring.golden \
		--repo-root .. \
		--captures-root "../$(or $(GOLDEN_CAPTURE_ROOT),data/captures/phase-1/goldens)" \
		--goldens-root ../lab/goldens/phase-1
	cd platform && PYTHONPATH=.. uv run python -m lab.scoring.decision_golden \
		--repo-root .. \
		--captures-root "../$(or $(GOLDEN_CAPTURE_ROOT),data/captures/phase-1/goldens)" \
		--goldens-root ../lab/goldens/phase-4

.PHONY: regen-golden
regen-golden: data-pull ## Rewrite reviewed goldens from development captures
	cd platform && PYTHONPATH=.. uv run python -m lab.scoring.golden \
		--repo-root .. \
		--captures-root "../$(or $(GOLDEN_CAPTURE_ROOT),data/captures/phase-1/goldens)" \
		--goldens-root ../lab/goldens/phase-1 --write
	cd platform && PYTHONPATH=.. uv run python -m lab.scoring.decision_golden \
		--repo-root .. \
		--captures-root "../$(or $(GOLDEN_CAPTURE_ROOT),data/captures/phase-1/goldens)" \
		--goldens-root ../lab/goldens/phase-4 --write
