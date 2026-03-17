#!/bin/bash
# ============================================================
# MediaWiki auto-configuration entrypoint
# ============================================================
# On ECS Fargate there is no `docker exec` — the container must
# self-configure on first boot.  This script:
#   1. Checks whether tables already exist (from a prior deploy)
#   2. If not, runs install.php to create schema + tables
#   3. Generates LocalSettings.php from template
#   4. Applies extensions & permissions via update.php
#
# Handles both fresh DB and existing DB (e.g. local Docker ran
# first against the same Lakebase instance).
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

    ENVSUBST_VARS='${LAKEBASE_HOST} ${LAKEBASE_PORT} ${LAKEBASE_DB} ${LAKEBASE_USER} ${LAKEBASE_PASSWORD} ${MW_SECRET_KEY} ${MW_UPGRADE_KEY} ${MW_SERVER_URL}'

    # Probe the DB: does the mediawiki.page table already exist?
    TABLE_EXISTS=$(php -r "
        \$c = @pg_connect('host=${LAKEBASE_HOST} port=${LAKEBASE_PORT:-5432} dbname=${LAKEBASE_DB:-wikidb} user=${LAKEBASE_USER} password=${LAKEBASE_PASSWORD}');
        if (!\$c) { echo 'error'; exit(0); }
        \$r = @pg_query(\$c, \"SELECT 1 FROM information_schema.tables WHERE table_schema='mediawiki' AND table_name='page' LIMIT 1\");
        echo (\$r && pg_num_rows(\$r) > 0) ? 'yes' : 'no';
    ")

    if [ "$TABLE_EXISTS" = "yes" ]; then
        echo "  ℹ️  Tables already exist — skipping install"
    elif [ "$TABLE_EXISTS" = "no" ]; then
        echo "  📦 Fresh database — running installer..."
        # LocalSettings.php must NOT exist — the installer refuses to run if it does.
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
            "${MW_ADMIN_USER:-Admin}"
    else
        echo "  ⚠️  Could not connect to database — attempting install anyway..."
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
            "${MW_ADMIN_USER:-Admin}"
    fi

    # Generate LocalSettings.php from our template (overwriting whatever
    # install.php may have created). Explicit var list so envsubst doesn't
    # destroy PHP variables like $IP, $wgSitename, etc.
    envsubst "$ENVSUBST_VARS" < "$TEMPLATE" > "$SETTINGS"

    # Apply extensions and permissions from our config
    php maintenance/run.php update --quick

    echo "✅ MediaWiki auto-configuration complete."
fi

exec "$@"
