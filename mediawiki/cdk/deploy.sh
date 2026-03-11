#!/usr/bin/env bash
# ============================================================
# Deploy MediaWiki to AWS ECS Fargate via CDK
# ============================================================
# Reads Lakebase credentials directly from the Databricks secret
# scope (wiki-rag), syncs them to AWS Secrets Manager, and runs
# cdk deploy. No .env file required.
#
# Usage:
#   ./deploy.sh                            # default profiles
#   AWS_PROFILE=my-aws ./deploy.sh         # specific AWS profile
#   DATABRICKS_CONFIG_PROFILE=my-db \
#     AWS_PROFILE=my-aws ./deploy.sh       # both profiles explicit
#
# Prerequisites:
#   - Databricks CLI authenticated (secrets already populated via
#     'make setup-secrets' + 'make setup-lakebase')
#   - AWS CLI configured (aws sts get-caller-identity)
#   - AWS CDK installed (npm install -g aws-cdk)
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCOPE="wiki-rag"
AWS_SECRET_NAME="wiki-rag/mediawiki"

# -------------------------------------------------------
# 0. Validate prerequisites
# -------------------------------------------------------
for cmd in aws cdk python3 databricks; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "❌ Required command '$cmd' not found"
        exit 1
    fi
done

[ -n "${AWS_PROFILE:-}" ]                  && echo "☁️  AWS Profile:        $AWS_PROFILE"
[ -n "${DATABRICKS_CONFIG_PROFILE:-}" ]    && echo "🔷 Databricks Profile: $DATABRICKS_CONFIG_PROFILE"

aws sts get-caller-identity > /dev/null 2>&1 \
    || { echo "❌ AWS not authenticated. Run: aws configure"; exit 1; }

# -------------------------------------------------------
# 1. Read credentials from Databricks Secrets
# -------------------------------------------------------
echo ""
echo "🔷 Reading credentials from Databricks secret scope '${SCOPE}'..."

get_secret() {
    databricks secrets get-secret "${SCOPE}" "$1" -o json 2>/dev/null \
        | python3 -c "import sys,json,base64; print(base64.b64decode(json.load(sys.stdin)['value']).decode())" 2>/dev/null \
        || echo ""
}

LAKEBASE_HOST=$(get_secret lakebase_host)
LAKEBASE_PORT=$(get_secret lakebase_port)
LAKEBASE_DB=$(get_secret lakebase_db)
LAKEBASE_USER=$(get_secret mw_role)
MW_PASSWORD=$(get_secret mw_password)

[ -z "$LAKEBASE_PORT" ] && LAKEBASE_PORT="5432"
[ -z "$LAKEBASE_DB" ]   && LAKEBASE_DB="wikidb"
[ -z "$LAKEBASE_USER" ] && LAKEBASE_USER="mediawiki"

if [ -z "$LAKEBASE_HOST" ] || [ -z "$MW_PASSWORD" ]; then
    echo "❌ Required secrets not found in scope '${SCOPE}'."
    echo "   Run 'make setup-secrets' and 'make setup-lakebase' first."
    exit 1
fi

# Generate MW-specific secrets (deterministic from password for idempotency)
MW_SECRET_KEY=$(echo -n "${MW_PASSWORD}:secret_key" | openssl dgst -sha256 -hex | awk '{print $NF}')
MW_UPGRADE_KEY=$(echo -n "${MW_PASSWORD}:upgrade_key" | openssl dgst -sha256 -hex | awk '{print substr($NF,1,32)}')

echo "  ✅ Credentials loaded (host: ${LAKEBASE_HOST})"

# -------------------------------------------------------
# 2. Sync to AWS Secrets Manager
# -------------------------------------------------------
SECRET_VALUE=$(python3 -c "
import json, sys
print(json.dumps({
    'mw_password': sys.argv[1],
    'mw_secret_key': sys.argv[2],
    'mw_upgrade_key': sys.argv[3],
}))" "$MW_PASSWORD" "$MW_SECRET_KEY" "$MW_UPGRADE_KEY")

echo ""
echo "🔑 Syncing to AWS Secrets Manager (${AWS_SECRET_NAME})..."
if aws secretsmanager describe-secret --secret-id "$AWS_SECRET_NAME" > /dev/null 2>&1; then
    aws secretsmanager update-secret \
        --secret-id "$AWS_SECRET_NAME" \
        --secret-string "$SECRET_VALUE" > /dev/null
    echo "  ✅ Secret updated"
else
    aws secretsmanager create-secret \
        --name "$AWS_SECRET_NAME" \
        --secret-string "$SECRET_VALUE" > /dev/null
    echo "  ✅ Secret created"
fi

# -------------------------------------------------------
# 3. Setup Python venv + CDK deps
# -------------------------------------------------------
cd "$SCRIPT_DIR"
if [ ! -d .venv ]; then
    echo ""
    echo "📦 Creating Python virtual environment..."
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

# -------------------------------------------------------
# 4. Deploy CDK stack
# -------------------------------------------------------
echo ""
echo "🚀 Deploying CDK stack..."
cdk deploy \
    -c lakebase_host="$LAKEBASE_HOST" \
    -c lakebase_port="$LAKEBASE_PORT" \
    -c lakebase_db="$LAKEBASE_DB" \
    -c lakebase_user="$LAKEBASE_USER" \
    -c mw_admin_user="Admin" \
    -c secret_name="$AWS_SECRET_NAME" \
    --require-approval never

# -------------------------------------------------------
# 5. Print result
# -------------------------------------------------------
echo ""
echo "========================================="
echo "  ✅ MediaWiki deployed to ECS Fargate!"
echo ""
echo "  Copy the MediaWikiUrl from the output above and:"
echo ""
echo "  1. Set MEDIAWIKI_URL in your shell:"
echo "     export MEDIAWIKI_URL=http://<alb-dns-name>"
echo ""
echo "  2. Or add to databricks.yml variables:"
echo "     mediawiki_url:"
echo "       default: \"http://<alb-dns-name>\""
echo ""
echo "  Then ingest data:"
echo "     MEDIAWIKI_URL=\$MEDIAWIKI_URL make demo-load"
echo "========================================="
