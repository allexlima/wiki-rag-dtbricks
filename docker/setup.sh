#!/usr/bin/env bash
# ============================================================
# 🚀 Bootstrap MediaWiki with Lakebase PostgreSQL backend
# ============================================================
#
# Flow:
#   1. Validate .env credentials
#   2. Build & start container (clean — no LocalSettings.php)
#   3. Run MediaWiki install (creates mediawiki schema + tables)
#   4. Generate LocalSettings.php from template
#   5. Copy it into the running container
#   6. Run update to apply our config (extensions, permissions)
#   7. Verify connectivity
#
# Prerequisites:
#   - docker & docker compose
#   - envsubst (from gettext)
#   - Lakebase instance provisioned (run notebook 00_setup_lakebase first)
#   - .env file with credentials (see .env.example)
#
# Usage: ./setup.sh
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

CONTAINER_NAME="wiki-rag-mediawiki"
MW_SETTINGS_PATH="/var/www/html/LocalSettings.php"

# -------------------------------------------------------
# 1. Load & validate .env
# -------------------------------------------------------
if [ ! -f .env ]; then
    echo "❌ .env not found. Copy the template and fill in your credentials:"
    echo "   cp .env.example .env"
    exit 1
fi

# shellcheck disable=SC1091
set -a
source .env
set +a

REQUIRED_VARS=(
    LAKEBASE_HOST LAKEBASE_PORT LAKEBASE_DB LAKEBASE_USER LAKEBASE_PASSWORD
    MW_ADMIN_USER MW_ADMIN_PASSWORD MW_SECRET_KEY MW_UPGRADE_KEY
)
for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var:-}" ]; then
        echo "❌ Required variable '$var' is not set in .env"
        exit 1
    fi
done
echo "✅ .env validated"

# -------------------------------------------------------
# 2. Build & start container
# -------------------------------------------------------
echo "🏗️  Building & starting container..."
docker compose up -d --build
echo "⏳ Waiting for Apache to start..."
sleep 5

# -------------------------------------------------------
# 3. Install MediaWiki (creates mediawiki schema + tables)
# -------------------------------------------------------
# The container starts clean (no LocalSettings.php), so the installer
# proceeds without the "already installed" guard. On re-runs it detects
# existing tables and exits cleanly.
echo "📦 Running MediaWiki install..."
docker exec "$CONTAINER_NAME" php maintenance/run.php install \
    --dbtype=postgres \
    --dbserver="${LAKEBASE_HOST}" \
    --dbport="${LAKEBASE_PORT}" \
    --dbname="${LAKEBASE_DB}" \
    --dbuser="${LAKEBASE_USER}" \
    --dbpass="${LAKEBASE_PASSWORD}" \
    --installdbuser="${LAKEBASE_USER}" \
    --installdbpass="${LAKEBASE_PASSWORD}" \
    --pass="${MW_ADMIN_PASSWORD}" \
    --scriptpath="" \
    --server="http://localhost:8080" \
    --skins=Vector \
    "Wiki RAG Demo" \
    "${MW_ADMIN_USER}" \
    || echo "  ⚠️  install exited non-zero (tables may already exist — continuing)"

# -------------------------------------------------------
# 4. Generate LocalSettings.php from template
# -------------------------------------------------------
echo "📝 Generating LocalSettings.php..."
ENVSUBST_VARS='${LAKEBASE_HOST} ${LAKEBASE_PORT} ${LAKEBASE_DB} ${LAKEBASE_USER} ${LAKEBASE_PASSWORD} ${MW_SECRET_KEY} ${MW_UPGRADE_KEY}'
envsubst "$ENVSUBST_VARS" < LocalSettings.php.template > LocalSettings.php
echo "  ✅ LocalSettings.php created"

# -------------------------------------------------------
# 5. Copy config into the running container
# -------------------------------------------------------
echo "📋 Deploying LocalSettings.php into container..."
docker cp LocalSettings.php "$CONTAINER_NAME:$MW_SETTINGS_PATH"
echo "  ✅ Config deployed"

# -------------------------------------------------------
# 6. Run update to apply extensions & permissions from our config
# -------------------------------------------------------
echo "🔄 Running MediaWiki update..."
docker exec "$CONTAINER_NAME" php maintenance/run.php update --quick \
    || echo "  ⚠️  update returned non-zero — check logs if issues arise"

# -------------------------------------------------------
# 7. Verify
# -------------------------------------------------------
echo ""
echo "========================================="
echo "  ✅ MediaWiki is ready!"
echo "  🌐 URL:   http://localhost:8080"
echo "  👤 Admin: ${MW_ADMIN_USER}"
echo "========================================="
