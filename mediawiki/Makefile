# 🐳 Docker — MediaWiki + dataset management
# Usage: make up | make ingest | make clean | make down | make help

SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: up
up:  ## 🏗️  Bootstrap MediaWiki container (build, install, configure)
	@chmod +x scripts/setup.sh && ./scripts/setup.sh

.PHONY: down
down:  ## 🛑 Stop and remove MediaWiki container + volumes
	@echo "🛑 Stopping MediaWiki..."
	@docker compose down -v
	@echo "✅ MediaWiki stopped"

.PHONY: ingest
ingest:  ## 📥 Ingest a dataset into MediaWiki (interactive selector)
	@chmod +x scripts/select_dataset.sh scripts/ingest.sh && ./scripts/select_dataset.sh

.PHONY: clean
clean:  ## 🧹 Delete all wiki pages and uploaded files from MediaWiki
	@chmod +x scripts/clean.sh && ./scripts/clean.sh

.PHONY: help
help:  ## ❓ Show available targets
	@echo ""
	@printf "  🐳 \033[1mDocker — MediaWiki\033[0m\n"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""
