#!/usr/bin/env bash
# ============================================================
# Bootstrap MediaWiki with Lakebase PostgreSQL backend
# ============================================================
#
# Flow:
#   0. Auto-generate .env from Databricks secrets (if not present)
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
#   - Lakebase instance provisioned (run 'make setup-lakebase' first)
#   - EITHER: .env file already exists
#     OR:     Databricks CLI authenticated (secrets will be read automatically)
#
# Usage: ./setup.sh
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

CONTAINER_NAME="wiki-rag-mediawiki"
MW_SETTINGS_PATH="/var/www/html/LocalSettings.php"
SCOPE="wiki-rag"

# -------------------------------------------------------
# 0. Auto-generate .env from Databricks secrets (if missing)
# -------------------------------------------------------
if [ ! -f .env ]; then
    echo "📋 No .env found — generating from Databricks secrets..."
    if [ -n "${DATABRICKS_CONFIG_PROFILE:-}" ]; then
        echo "   Profile: $DATABRICKS_CONFIG_PROFILE"
    fi

    if ! command -v databricks &>/dev/null; then
        echo "❌ .env not found and 'databricks' CLI not available."
        echo "   Either create .env manually (see .env.example) or install Databricks CLI."
        exit 1
    fi

    # Helper: read a secret and base64-decode the value from the JSON response
    get_secret() {
        databricks secrets get-secret "${SCOPE}" "$1" -o json 2>/dev/null \
            | python3 -c "import sys,json,base64; print(base64.b64decode(json.load(sys.stdin)['value']).decode())" 2>/dev/null \
            || echo ""
    }

    LAKEBASE_HOST=$(get_secret lakebase_host)
    LAKEBASE_PORT=$(get_secret lakebase_port)
    LAKEBASE_DB=$(get_secret lakebase_db)
    MW_ROLE=$(get_secret mw_role)
    MW_PASSWORD=$(get_secret mw_password)

    [ -z "$LAKEBASE_PORT" ] && LAKEBASE_PORT="5432"
    [ -z "$LAKEBASE_DB" ] && LAKEBASE_DB="wikidb"
    [ -z "$MW_ROLE" ] && MW_ROLE="mediawiki"

    if [ -z "${LAKEBASE_HOST}" ] || [ -z "${MW_PASSWORD}" ]; then
        echo "❌ Required secrets not found in scope '${SCOPE}'."
        echo "   Run 'make setup-secrets' and 'make setup-lakebase' first."
        echo "   If using a non-default profile: export PROFILE=my-workspace"
        exit 1
    fi

    MW_SECRET_KEY=$(openssl rand -hex 32)
    MW_UPGRADE_KEY=$(openssl rand -hex 16)

    cat > .env <<EOF
# Auto-generated from Databricks secrets — regenerate by deleting this file and re-running setup.sh

# Lakebase connection (mediawiki role — static password)
LAKEBASE_HOST=${LAKEBASE_HOST}
LAKEBASE_PORT=${LAKEBASE_PORT}
LAKEBASE_DB=${LAKEBASE_DB}
LAKEBASE_USER=${MW_ROLE}
LAKEBASE_PASSWORD=${MW_PASSWORD}

# MediaWiki admin
MW_ADMIN_USER=Admin
MW_ADMIN_PASSWORD=${MW_PASSWORD}

# MediaWiki secrets
MW_SECRET_KEY=${MW_SECRET_KEY}
MW_UPGRADE_KEY=${MW_UPGRADE_KEY}
EOF

    echo "  ✅ .env generated from Databricks secrets"
else
    echo "✅ Using existing .env"
fi

# -------------------------------------------------------
# 1. Load & validate .env
# -------------------------------------------------------
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
: "${MW_SERVER_URL:=http://localhost:8080}"
export MW_SERVER_URL
ENVSUBST_VARS='${LAKEBASE_HOST} ${LAKEBASE_PORT} ${LAKEBASE_DB} ${LAKEBASE_USER} ${LAKEBASE_PASSWORD} ${MW_SECRET_KEY} ${MW_UPGRADE_KEY} ${MW_SERVER_URL}'
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
