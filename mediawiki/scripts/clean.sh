#!/usr/bin/env bash
# ============================================================
# 🧹 Clean MediaWiki — delete all ingested pages and files
# ============================================================
#
# Reverses what ingest.sh does: deletes all wiki pages (except
# special pages) and all uploaded files via the MediaWiki API.
#
# Usage: ./clean.sh
#
# Prerequisites:
#   - MediaWiki container running
#   - mediawiki/.env with MW_ADMIN_USER and MW_ADMIN_PASSWORD
#   - jq, curl installed
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DOCKER_DIR="$SCRIPT_DIR/.."
API_URL="${MEDIAWIKI_URL:-http://localhost:8080}/api.php"
COOKIE_JAR=$(mktemp)
trap 'rm -f "$COOKIE_JAR"' EXIT

for cmd in curl jq; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "❌ Required command '$cmd' not found"
        exit 1
    fi
done

# -------------------------------------------------------
# 1. Load .env
# -------------------------------------------------------
ENV_FILE="$DOCKER_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "❌ mediawiki/.env not found"
    exit 1
fi

set -a
source "$ENV_FILE"
set +a

: "${MW_ADMIN_USER:?MW_ADMIN_USER not set in .env}"
: "${MW_ADMIN_PASSWORD:?MW_ADMIN_PASSWORD not set in .env}"

# -------------------------------------------------------
# 2. Login
# -------------------------------------------------------
echo "🔑 Logging into MediaWiki as '${MW_ADMIN_USER}'..."

LOGIN_TOKEN=$(curl -s -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
    "${API_URL}?action=query&meta=tokens&type=login&format=json" \
    | jq -r '.query.tokens.logintoken')

LOGIN_RESULT=$(curl -s -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
    -X POST "$API_URL" \
    --data-urlencode "action=login" \
    --data-urlencode "lgname=${MW_ADMIN_USER}" \
    --data-urlencode "lgpassword=${MW_ADMIN_PASSWORD}" \
    --data-urlencode "lgtoken=${LOGIN_TOKEN}" \
    --data-urlencode "format=json")

LOGIN_STATUS=$(echo "$LOGIN_RESULT" | jq -r '.login.result')
if [ "$LOGIN_STATUS" != "Success" ]; then
    echo "❌ Login failed: $LOGIN_RESULT"
    exit 1
fi
echo "  ✅ Logged in"

CSRF_TOKEN=$(curl -s -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
    "${API_URL}?action=query&meta=tokens&format=json" \
    | jq -r '.query.tokens.csrftoken')

# -------------------------------------------------------
# 3. Parallel delete infrastructure
# -------------------------------------------------------
PARALLEL_JOBS=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
PROGRESS_DIR=$(mktemp -d)
DELETE_SCRIPT=$(mktemp)

cat > "$DELETE_SCRIPT" << 'DELETE_EOF'
#!/usr/bin/env bash
title="$1"
cookie_jar="$2"
api_url="$3"
csrf_token="$4"
progress_dir="$5"

curl -s -b "$cookie_jar" \
    -X POST "$api_url" \
    --data-urlencode "action=delete" \
    --data-urlencode "title=${title}" \
    --data-urlencode "reason=Cleanup via clean.sh" \
    --data-urlencode "token=${csrf_token}" \
    --data-urlencode "format=json" > /dev/null

# Touch marker file (atomic, race-free)
safe_name=$(echo "$title" | tr '/:' '__')
touch "${progress_dir}/${safe_name}.done"
DELETE_EOF
chmod +x "$DELETE_SCRIPT"

draw_progress() {
    local current=$1 total=$2 label=$3 width=40
    local pct=$((current * 100 / total))
    local filled=$((current * width / total))
    local empty=$((width - filled))
    local bar=""
    for ((i = 0; i < filled; i++)); do bar+="█"; done
    for ((i = 0; i < empty; i++)); do bar+="░"; done
    printf "\r  %s [%s] %d/%d (%d%%)" "$label" "$bar" "$current" "$total" "$pct"
}

parallel_delete() {
    local label="$1" total="$2"
    shift 2

    # Clean progress dir
    rm -f "$PROGRESS_DIR"/*.done 2>/dev/null
    draw_progress 0 "$total" "$label"

    printf '%s\n' "$@" \
        | xargs -P "$PARALLEL_JOBS" -I{} \
            bash "$DELETE_SCRIPT" {} "$COOKIE_JAR" "$API_URL" "$CSRF_TOKEN" "$PROGRESS_DIR" &
    local pid=$!

    while kill -0 "$pid" 2>/dev/null; do
        local done_count
        done_count=$(find "$PROGRESS_DIR" -name "*.done" 2>/dev/null | wc -l | tr -d ' ')
        draw_progress "$done_count" "$total" "$label"
        sleep 0.5
    done
    wait "$pid" || true

    draw_progress "$total" "$total" "$label"
    echo ""
}

# -------------------------------------------------------
# 4. Delete all uploaded files (parallel + progress)
# -------------------------------------------------------
echo ""
echo "🗑️  Collecting uploaded files..."

ALL_FILES=()
CONTINUE=""
while true; do
    QUERY="${API_URL}?action=query&list=allimages&ailimit=50&format=json"
    [ -n "$CONTINUE" ] && QUERY+="&aicontinue=${CONTINUE}"
    RESULT=$(curl -s -b "$COOKIE_JAR" -c "$COOKIE_JAR" "$QUERY")
    while IFS= read -r name; do
        [ -n "$name" ] && ALL_FILES+=("File:${name}")
    done <<< "$(echo "$RESULT" | jq -r '.query.allimages[].name // empty')"
    CONTINUE=$(echo "$RESULT" | jq -r '.continue.aicontinue // empty')
    [ -z "$CONTINUE" ] && break
done

FILE_COUNT=${#ALL_FILES[@]}
if [ "$FILE_COUNT" -gt 0 ]; then
    parallel_delete "🗑️ Files" "$FILE_COUNT" "${ALL_FILES[@]}"
fi
echo "  ✅ Deleted ${FILE_COUNT} files"

# -------------------------------------------------------
# 5. Delete all content pages (parallel + progress)
# -------------------------------------------------------
echo ""
echo "🗑️  Collecting wiki pages..."

ALL_PAGES=()
CONTINUE=""
while true; do
    QUERY="${API_URL}?action=query&list=allpages&apnamespace=0&aplimit=50&format=json"
    [ -n "$CONTINUE" ] && QUERY+="&apcontinue=${CONTINUE}"
    RESULT=$(curl -s -b "$COOKIE_JAR" -c "$COOKIE_JAR" "$QUERY")
    while IFS= read -r title; do
        [ -n "$title" ] && ALL_PAGES+=("$title")
    done <<< "$(echo "$RESULT" | jq -r '.query.allpages[].title // empty')"
    CONTINUE=$(echo "$RESULT" | jq -r '.continue.apcontinue // empty')
    [ -z "$CONTINUE" ] && break
done

PAGE_COUNT=${#ALL_PAGES[@]}
if [ "$PAGE_COUNT" -gt 0 ]; then
    parallel_delete "🗑️ Pages" "$PAGE_COUNT" "${ALL_PAGES[@]}"
fi
echo "  ✅ Deleted ${PAGE_COUNT} pages"

rm -rf "$PROGRESS_DIR" "$DELETE_SCRIPT"

# -------------------------------------------------------
# Done
# -------------------------------------------------------
echo ""
echo "========================================="
echo "  🧹 Cleaned: ${PAGE_COUNT} pages + ${FILE_COUNT} files"
echo "  🌐 MediaWiki is now empty"
echo "========================================="
