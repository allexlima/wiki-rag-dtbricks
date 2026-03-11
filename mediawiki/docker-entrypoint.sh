#!/bin/bash
# ============================================================
# MediaWiki auto-configuration entrypoint
# ============================================================
# On ECS Fargate there is no `docker exec` — the container must
# self-configure on first boot.  This script:
#   1. Generates LocalSettings.php from the template (envsubst)
#   2. Runs the MediaWiki installer (creates schema + tables)
#   3. Applies extensions & permissions via update.php
#
# In local Docker mode, setup.sh copies LocalSettings.php into
# the container AFTER build, so the condition below is false
# and this entrypoint is a no-op.
# ============================================================
set -e

SETTINGS="/var/www/html/LocalSettings.php"
TEMPLATE="/var/www/html/LocalSettings.php.template"

# ECS mode: auto-configure if env vars present and no LocalSettings.php
if [ ! -f "$SETTINGS" ] && [ -n "${LAKEBASE_HOST:-}" ]; then
    echo "🚀 ECS mode: auto-configuring MediaWiki..."

    : "${MW_SERVER_URL:=http://localhost}"
    export MW_SERVER_URL

    # Generate LocalSettings.php from template (explicit var list — bare envsubst
    # would destroy PHP variables like $IP, $wgSitename, etc.)
    ENVSUBST_VARS='${LAKEBASE_HOST} ${LAKEBASE_PORT} ${LAKEBASE_DB} ${LAKEBASE_USER} ${LAKEBASE_PASSWORD} ${MW_SECRET_KEY} ${MW_UPGRADE_KEY} ${MW_SERVER_URL}'
    envsubst "$ENVSUBST_VARS" < "$TEMPLATE" > "$SETTINGS"

    # Run MediaWiki installer (creates mediawiki schema + tables)
    php maintenance/run.php install \
        --dbtype=postgres \
        --dbserver="$LAKEBASE_HOST" \
        --dbport="${LAKEBASE_PORT:-5432}" \
        --dbname="${LAKEBASE_DB:-wikidb}" \
        --dbuser="$LAKEBASE_USER" \
        --dbpass="$LAKEBASE_PASSWORD" \
        --installdbuser="$LAKEBASE_USER" \
        --installdbpass="$LAKEBASE_PASSWORD" \
        --pass="${MW_ADMIN_PASSWORD}" \
        --scriptpath="" \
        --server="$MW_SERVER_URL" \
        --skins=Vector \
        "Wiki RAG Demo" \
        "${MW_ADMIN_USER:-Admin}" \
        || echo "  ⚠️  Install skipped (tables may already exist)"

    # Overwrite with our template (installer generates its own LocalSettings.php)
    envsubst "$ENVSUBST_VARS" < "$TEMPLATE" > "$SETTINGS"

    # Apply extensions and permissions from our config
    php maintenance/run.php update --quick \
        || echo "  ⚠️  Update returned non-zero"

    echo "✅ MediaWiki auto-configuration complete."
fi

exec "$@"
