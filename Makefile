# Wiki RAG on Databricks — deployment automation
# Usage: make deploy | make destroy-all | make help
#
# Override target: make deploy TARGET=prod

SHELL := /bin/bash
.DEFAULT_GOAL := help
TARGET ?= dev

# ---------- Pre-flight ----------

.PHONY: validate
validate:  ## Check prerequisites (Databricks CLI, Docker, auth)
	@echo "=== Pre-flight checks ==="
	@command -v databricks >/dev/null 2>&1 || { echo "ERROR: 'databricks' CLI not found"; exit 1; }
	@command -v docker >/dev/null 2>&1 || { echo "ERROR: 'docker' not found"; exit 1; }
	@databricks auth env >/dev/null 2>&1 || { echo "ERROR: Databricks CLI not authenticated. Run 'databricks auth login'"; exit 1; }
	@databricks bundle validate -t $(TARGET) >/dev/null 2>&1 || { echo "ERROR: Bundle validation failed"; databricks bundle validate -t $(TARGET); exit 1; }
	@echo "=== All checks passed ==="

# ---------- Secrets (one-time, interactive) ----------

.PHONY: setup-secrets
setup-secrets:  ## Create secret scope and store Lakebase password (interactive, one-time)
	python src/setup_secrets.py

# ---------- Infrastructure ----------

.PHONY: setup-lakebase
setup-lakebase: validate  ## Provision Lakebase instance, create DB, role, DDL (runs DAB job)
	databricks bundle deploy -t $(TARGET)
	databricks bundle run setup_lakebase -t $(TARGET)

# ---------- MediaWiki (local Docker) ----------

.PHONY: docker-up
docker-up:  ## Start MediaWiki container (auto-generates .env if missing)
	cd docker && chmod +x setup.sh && ./setup.sh

.PHONY: docker-down
docker-down:  ## Stop and remove MediaWiki container + volumes
	cd docker && docker compose down -v

# ---------- Data ----------

.PHONY: ingest
ingest: validate  ## Run ingestion pipeline (reads MW, chunks, embeds)
	databricks bundle run wiki_rag_ingestion -t $(TARGET)

# ---------- Model + Endpoint ----------

.PHONY: deploy-agent
deploy-agent: validate  ## Log model to MLflow, register in UC, deploy serving endpoint
	databricks bundle deploy -t $(TARGET)
	databricks bundle run deploy_agent -t $(TARGET)

# ---------- Full Stack ----------

.PHONY: deploy
deploy: setup-lakebase docker-up deploy-agent ingest bundle-deploy  ## Full deployment (all steps)
	@echo ""
	@echo "=== Deployment complete ==="
	@echo "  Streamlit app:  databricks apps get wiki-rag-app"
	@echo "  Serving endpoint: databricks serving-endpoints get wiki-rag-endpoint"

.PHONY: bundle-deploy
bundle-deploy: validate  ## Deploy DAB resources (app, job schedules)
	databricks bundle deploy -t $(TARGET)

# ---------- Teardown ----------

.PHONY: destroy
destroy:  ## Tear down Databricks resources (keeps Lakebase + Docker)
	databricks bundle destroy -t $(TARGET) --auto-approve || true
	@echo ""
	@echo "NOTE: Lakebase instance and secrets are NOT destroyed (manual cleanup if needed)"
	@echo "NOTE: Docker containers are NOT stopped (run: make docker-down)"

.PHONY: destroy-all
destroy-all: destroy docker-down  ## Tear down everything (Databricks + Docker)
	@echo "=== All resources destroyed ==="

# ---------- Help ----------

.PHONY: help
help:  ## Show available targets
	@echo "Wiki RAG on Databricks — Deployment Targets"
	@echo ""
	@echo "  TARGET=$(TARGET) (override with TARGET=prod)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
