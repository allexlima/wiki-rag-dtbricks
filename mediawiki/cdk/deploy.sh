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

# Resolve Databricks profile: PROFILE (Makefile convention) or DATABRICKS_CONFIG_PROFILE
DB_PROFILE="${DATABRICKS_CONFIG_PROFILE:-${PROFILE:-}}"
[ -n "$DB_PROFILE" ] && export DATABRICKS_CONFIG_PROFILE="$DB_PROFILE"
DB_PROFILE_FLAG="${DB_PROFILE:+--profile $DB_PROFILE}"

# Default AWS region to us-east-1 if not configured
if [ -z "${AWS_DEFAULT_REGION:-}" ] && [ -z "${AWS_REGION:-}" ] && ! aws configure get region >/dev/null 2>&1; then
    export AWS_DEFAULT_REGION="us-east-1"
fi

[ -n "${AWS_PROFILE:-}" ] && echo "☁️  AWS Profile:        $AWS_PROFILE"
[ -n "$DB_PROFILE" ]      && echo "🔷 Databricks Profile: $DB_PROFILE"

aws sts get-caller-identity > /dev/null 2>&1 \
    || { echo "❌ AWS not authenticated. Run: aws configure"; exit 1; }

# -------------------------------------------------------
# 1. Read credentials from Databricks Secrets
# -------------------------------------------------------
echo ""
echo "🔷 Reading credentials from Databricks secret scope '${SCOPE}'..."

get_secret() {
    databricks secrets get-secret "${SCOPE}" "$1" $DB_PROFILE_FLAG -o json 2>/dev/null \
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
pip install -q --upgrade pip
pip install -q -r requirements.txt
npm install -g aws-cdk@latest --silent 2>/dev/null

# -------------------------------------------------------
# 4. Bootstrap CDK (skip if already bootstrapped)
# -------------------------------------------------------
echo ""
if aws cloudformation describe-stacks --stack-name CDKToolkit >/dev/null 2>&1; then
    echo "🏗️  CDK already bootstrapped — skipping"
else
    echo "🏗️  Bootstrapping CDK..."
    cdk bootstrap --require-approval never
fi

# -------------------------------------------------------
# 5. Deploy CDK stack
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
# 6. Read ALB URL from CDK outputs
# -------------------------------------------------------
MW_URL=$(aws cloudformation describe-stacks --stack-name WikiRagMediaWiki \
    --query "Stacks[0].Outputs[?OutputKey=='MediaWikiUrl'].OutputValue" --output text 2>/dev/null)
NAT_IP=$(aws cloudformation describe-stacks --stack-name WikiRagMediaWiki \
    --query "Stacks[0].Outputs[?OutputKey=='NatElasticIp'].OutputValue" --output text 2>/dev/null)

echo ""
echo "========================================="
echo "  ✅ MediaWiki deployed to ECS Fargate!"
echo ""
echo "  🌐 ${MW_URL}"
echo ""
echo "  Export for this session:"
echo "     export MEDIAWIKI_URL=${MW_URL}"
echo ""
echo "  Then ingest data:"
echo "     make demo-load"
if [ -n "$NAT_IP" ]; then
echo ""
echo "  🔒 NAT Elastic IP: ${NAT_IP}"
echo "     Add ${NAT_IP}/32 to Databricks workspace IP ACL"
echo "     for Lakebase connectivity."
fi
echo "========================================="
