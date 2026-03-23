# 🧠 Wiki RAG on Databricks — deployment automation
# Usage: make deploy | make destroy | make help
#
# Override target:  make deploy TARGET=prod
# Override profile: make deploy PROFILE=my-workspace

SHELL := /bin/bash
.DEFAULT_GOAL := help
TARGET ?= dev
PROFILE ?=

# ─── Config (matches databricks.yml defaults) ────────────────
SECRET_SCOPE  := wiki-rag
INSTANCE_NAME := wiki-rag-lakebase
ENDPOINT_NAME := wiki-rag-endpoint
CATALOG       := allex_workspace_catalog
SCHEMA        := wiki_rag
MODEL_NAME    := $(CATALOG).$(SCHEMA).wiki_rag_agent

# ─── CLI flags ────────────────────────────────────────────────
CLI_FLAGS := -t $(TARGET)
PROFILE_FLAG :=
ifdef PROFILE
CLI_FLAGS    += --profile $(PROFILE)
PROFILE_FLAG := --profile $(PROFILE)
endif

# Pass MEDIAWIKI_URL to databricks.yml variable when exported
BUNDLE_VARS :=
ifdef MEDIAWIKI_URL
BUNDLE_VARS += --var mediawiki_url=$(MEDIAWIKI_URL)
endif

# ─── Internal helpers ─────────────────────────────────────────

.PHONY: _check-cli _check-auth _require-secrets

_check-cli:
	@command -v databricks >/dev/null 2>&1 || { echo "❌ 'databricks' CLI not found"; exit 1; }

_check-auth: _check-cli
	@databricks auth env $(PROFILE_FLAG) >/dev/null 2>&1 \
		|| { echo "❌ Not authenticated. Run: databricks auth login $(PROFILE_FLAG)"; exit 1; }

_require-secrets: _check-auth
	@databricks secrets get-secret $(SECRET_SCOPE) mw_password $(PROFILE_FLAG) >/dev/null 2>&1 \
		|| { echo "❌ Secret 'mw_password' not found in scope '$(SECRET_SCOPE)'. Run 'make setup-secrets' first."; exit 1; }

# ─────────────────────────────────────────────────────────────
# 🔍 Pre-flight
# ─────────────────────────────────────────────────────────────

.PHONY: validate
validate: _check-auth  ## 🔍 Check prerequisites (Databricks CLI, auth, Docker)
	@echo "🔍 Running pre-flight checks..."
	@command -v docker >/dev/null 2>&1 || { echo "❌ 'docker' not found"; exit 1; }
	@echo "✅ All checks passed"

# ─────────────────────────────────────────────────────────────
# 🔑 Secrets
# ─────────────────────────────────────────────────────────────

.PHONY: setup-secrets
setup-secrets: _check-auth  ## 🔑 Create secret scope + store Lakebase password (interactive, one-time)
	@echo "" && \
	echo "🔑 Wiki RAG — Secret Scope Setup" && \
	if [ -n "$(PROFILE)" ]; then echo "   Profile: $(PROFILE)"; fi && \
	echo "" && \
	echo "   Password requirements (Lakebase Autoscaling):" && \
	echo "   • At least 12 characters" && \
	echo "   • Mix of uppercase, lowercase, digits, and special chars (!@#$$%)" && \
	echo "" && \
	read -s -p "   Enter password for the 'mediawiki' Lakebase PG role: " PW && echo && \
	read -s -p "   Confirm password: " PW2 && echo && \
	if [ "$$PW" != "$$PW2" ]; then echo "   ❌ Passwords do not match"; exit 1; fi && \
	if [ $${#PW} -lt 12 ]; then echo "   ❌ Password must be at least 12 characters"; exit 1; fi && \
	if ! echo "$$PW" | grep -q '[A-Z]'; then echo "   ❌ Password must contain at least one uppercase letter"; exit 1; fi && \
	if ! echo "$$PW" | grep -q '[a-z]'; then echo "   ❌ Password must contain at least one lowercase letter"; exit 1; fi && \
	if ! echo "$$PW" | grep -q '[0-9]'; then echo "   ❌ Password must contain at least one digit"; exit 1; fi && \
	if ! echo "$$PW" | grep -q '[^a-zA-Z0-9]'; then echo "   ❌ Password must contain at least one special character"; exit 1; fi && \
	echo "" && \
	(databricks secrets create-scope $(SECRET_SCOPE) $(PROFILE_FLAG) 2>/dev/null \
	  && echo "   ✅ Created secret scope '$(SECRET_SCOPE)'" \
	  || echo "   ✅ Secret scope '$(SECRET_SCOPE)' already exists") && \
	databricks secrets put-secret $(SECRET_SCOPE) mw_password --string-value "$$PW" $(PROFILE_FLAG) && \
	echo "   ✅ Stored 'mw_password' in scope '$(SECRET_SCOPE)'" && \
	echo "" && \
	echo "🔑 Done. Run 'make setup-lakebase' next."

# ─────────────────────────────────────────────────────────────
# 🗄️  Infrastructure
# ─────────────────────────────────────────────────────────────

.PHONY: setup-lakebase
setup-lakebase: _require-secrets  ## 🗄️  Provision Lakebase instance + create DB, role, DDL
	@echo "🗄️  Deploying bundle and running Lakebase setup..."
	@databricks bundle deploy $(CLI_FLAGS) $(BUNDLE_VARS)
	@databricks bundle run setup_lakebase $(CLI_FLAGS) $(BUNDLE_VARS)
	@echo "✅ Lakebase setup complete"

# ─────────────────────────────────────────────────────────────
# 📖 MediaWiki
# ─────────────────────────────────────────────────────────────

.PHONY: setup-wiki
setup-wiki: _require-secrets  ## 📖 Start MediaWiki container (auto-generates .env if missing)
	@$(if $(PROFILE),export DATABRICKS_CONFIG_PROFILE=$(PROFILE) && ,)cd mediawiki && $(MAKE) --no-print-directory up

.PHONY: wiki-destroy
wiki-destroy:  ## 📖 Stop and remove MediaWiki container + volumes + generated files
	@docker rm -f wiki-rag-mediawiki 2>/dev/null || true
	@cd mediawiki && docker compose down -v --rmi local 2>/dev/null || true
	@rm -f mediawiki/.env mediawiki/LocalSettings.php
	@echo "✅ MediaWiki destroyed (containers, volumes, image, .env, LocalSettings.php)"

.PHONY: demo-load
demo-load:  ## 📖 Ingest demo dataset into MediaWiki (interactive selector)
	@cd mediawiki && $(MAKE) --no-print-directory ingest

.PHONY: demo-cleanup
demo-cleanup:  ## 📖 Delete all wiki pages and uploaded files
	@cd mediawiki && $(MAKE) --no-print-directory clean

# ─────────────────────────────────────────────────────────────
# 🤖 Model + Endpoint
# ─────────────────────────────────────────────────────────────

.PHONY: deploy-agent
deploy-agent: _require-secrets  ## 🤖 Log model to MLflow, register in UC, deploy serving endpoint
	@echo "🤖 Deploying RAG agent..."
	@databricks bundle deploy $(CLI_FLAGS) $(BUNDLE_VARS)
	@databricks bundle run deploy_agent $(CLI_FLAGS) $(BUNDLE_VARS)
	@echo "✅ Agent deployed"

# ─────────────────────────────────────────────────────────────
# 📊 Data
# ─────────────────────────────────────────────────────────────

.PHONY: ingest
ingest: _require-secrets  ## 📊 Run ingestion pipeline (reads MW → chunks → embeds)
	@echo "📊 Running ingestion pipeline..."
	@databricks bundle deploy $(CLI_FLAGS) $(BUNDLE_VARS)
	@databricks bundle run wiki_rag_ingestion $(CLI_FLAGS) $(BUNDLE_VARS)

# ─────────────────────────────────────────────────────────────
# 🚀 Full Stack
# ─────────────────────────────────────────────────────────────

.PHONY: deploy
deploy: setup-lakebase setup-wiki deploy-agent ingest  ## 🚀 Full deployment (all steps)
	@echo ""
	@echo "🎉 Deployment complete!"
	@echo "   🔌 Endpoint: databricks serving-endpoints get $(ENDPOINT_NAME)"
	@echo "   🧪 Test via Databricks AI Playground"

# ─────────────────────────────────────────────────────────────
# 💥 Teardown
# ─────────────────────────────────────────────────────────────

.PHONY: destroy
destroy: _check-auth  ## 💥 Destroy everything: endpoint + model + bundle + Docker + Lakebase + secrets
	@echo ""
	@echo "💥 Tearing down Wiki RAG..."
	@echo ""
	@echo "  🤖 Serving endpoint ($(ENDPOINT_NAME))..."
	@if databricks serving-endpoints delete $(ENDPOINT_NAME) $(PROFILE_FLAG) 2>/dev/null; then \
		echo "     ✅ Deleted"; \
	else \
		echo "     ⏭️  Not found (already deleted or never created)"; \
	fi
	@echo ""
	@echo "  🧠 UC registered model ($(MODEL_NAME))..."
	@if databricks registered-models delete $(MODEL_NAME) $(PROFILE_FLAG) 2>/dev/null; then \
		echo "     ✅ Deleted"; \
	else \
		echo "     ⏭️  Not found (already deleted or never created)"; \
	fi
	@echo ""
	@echo "  📦 Bundle resources (jobs, app, workspace files)..."
	@databricks bundle destroy $(CLI_FLAGS) $(BUNDLE_VARS) --auto-approve 2>&1 | sed 's/^/     /' || true
	@echo ""
	@echo "  📖 MediaWiki (containers + generated files)..."
	@docker rm -f wiki-rag-mediawiki 2>/dev/null || true
	@cd mediawiki && docker compose down -v --rmi local 2>&1 | sed 's/^/     /' || true
	@rm -f mediawiki/.env mediawiki/LocalSettings.php
	@echo "     ✅ Removed .env, LocalSettings.php, containers, volumes, and image"
	@echo ""
	@echo "  🗄️  Lakebase project ($(INSTANCE_NAME))..."
	@if databricks postgres delete-project projects/$(INSTANCE_NAME) $(PROFILE_FLAG) 2>/dev/null; then \
		echo "     ✅ Deleted"; \
	else \
		echo "     ⏭️  Not found (already deleted or never created)"; \
	fi
	@echo ""
	@printf "  📂 Also delete UC schema '$(CATALOG).$(SCHEMA)' (all tables + data)? [y/N] " && \
	read CONFIRM && \
	if [ "$$CONFIRM" = "y" ] || [ "$$CONFIRM" = "Y" ]; then \
		if databricks schemas delete $(CATALOG).$(SCHEMA) --force $(PROFILE_FLAG) 2>/dev/null; then \
			echo "     ✅ Schema deleted"; \
		else \
			echo "     ⏭️  Schema not found or could not be deleted"; \
		fi; \
	else \
		echo "     ⏭️  Skipped (schema preserved)"; \
	fi
	@echo ""
	@printf "  🔑 Also delete secret scope '$(SECRET_SCOPE)' (passwords, credentials)? [y/N] " && \
	read CONFIRM && \
	if [ "$$CONFIRM" = "y" ] || [ "$$CONFIRM" = "Y" ]; then \
		if databricks secrets delete-scope $(SECRET_SCOPE) $(PROFILE_FLAG) 2>/dev/null; then \
			echo "     ✅ Secret scope deleted"; \
		else \
			echo "     ⏭️  Secret scope not found"; \
		fi; \
	else \
		echo "     ⏭️  Skipped (secrets preserved)"; \
	fi
	@echo ""
	@echo "🏁 Teardown complete."

# ─────────────────────────────────────────────────────────────
# ❓ Help
# ─────────────────────────────────────────────────────────────

.PHONY: help
help:  ## ❓ Show available targets
	@echo ""
	@printf "  🧠 \033[1mWiki RAG on Databricks\033[0m\n"
	@echo ""
	@printf "  \033[2mTARGET\033[0m  = \033[1m$(TARGET)\033[0m    (override with TARGET=prod)\n"
	@printf "  \033[2mPROFILE\033[0m = \033[1m$(if $(PROFILE),$(PROFILE),<default>)\033[0m  (override with PROFILE=my-workspace)\n"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""
