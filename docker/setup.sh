#!/usr/bin/env bash
# Bootstrap script for MediaWiki with Lakebase backend.
# Usage: ./setup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# -------------------------------------------------------
# 1. Check .env file
# -------------------------------------------------------
if [ ! -f .env ]; then
    echo "ERROR: .env file not found. Copy .env.example to .env and fill in your credentials:"
    echo "  cp .env.example .env"
    exit 1
fi

# shellcheck disable=SC1091
source .env

# -------------------------------------------------------
# 2. Generate LocalSettings.php from template
# -------------------------------------------------------
echo "Generating LocalSettings.php from template..."
envsubst < LocalSettings.php.template > LocalSettings.php
echo "  -> LocalSettings.php created."

# -------------------------------------------------------
# 3. Start MediaWiki container
# -------------------------------------------------------
echo "Starting MediaWiki container..."
docker compose up -d

echo "Waiting for MediaWiki to start..."
sleep 10

# -------------------------------------------------------
# 4. Run MediaWiki database setup (creates tables in mediawiki schema)
# -------------------------------------------------------
echo "Running MediaWiki install/update to create database tables..."
docker exec wiki-rag-mediawiki php maintenance/run.php install \
    --dbtype=postgres \
    --dbserver="${LAKEBASE_HOST}:${LAKEBASE_PORT}" \
    --dbname="${LAKEBASE_DB}" \
    --dbuser="${LAKEBASE_USER}" \
    --dbpass="${LAKEBASE_PASSWORD}" \
    --installdbuser="${LAKEBASE_USER}" \
    --installdbpass="${LAKEBASE_PASSWORD}" \
    --pass=admin123 \
    --scriptpath="" \
    --server="http://localhost:8080" \
    "Wiki RAG Demo" \
    "Admin" \
    2>/dev/null || true

# Run update to ensure schema is current
docker exec wiki-rag-mediawiki php maintenance/run.php update --quick 2>/dev/null || true

echo ""
echo "========================================="
echo "  MediaWiki is ready at http://localhost:8080"
echo "  Admin user: Admin / admin123"
echo "========================================="
