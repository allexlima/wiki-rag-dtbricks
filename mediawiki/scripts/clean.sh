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
MW_TARGET="${MEDIAWIKI_URL:-http://localhost:8080}"
LOCAL_URL="http://localhost:8080"
ALSO_LOCAL=0
API_URL="${MW_TARGET}/api.php"
COOKIE_JAR=$(mktemp)
COOKIE_JAR_LOCAL=$(mktemp)
trap 'rm -f "$COOKIE_JAR" "$COOKIE_JAR_LOCAL"' EXIT

echo "🎯 Target: ${MW_TARGET}"
if [ "$MW_TARGET" != "$LOCAL_URL" ]; then
    echo "   (using MEDIAWIKI_URL from environment)"
    printf "📦 Also clean local Docker (%s)? [Y/n] " "$LOCAL_URL"
    read -r BOTH
    if [ "$BOTH" != "n" ] && [ "$BOTH" != "N" ]; then
        if curl -s --max-time 3 "${LOCAL_URL}/api.php?action=query&meta=siteinfo&format=json" >/dev/null 2>&1; then
            ALSO_LOCAL=1
            echo "   → Cleaning both remote and local"
        else
            echo "   ⚠️  Local Docker not reachable — cleaning remote only"
        fi
    else
        echo "   → Cleaning remote only"
    fi
fi

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
# 2. Login (with retry — ECS sessions can be flaky after scale-up)
# -------------------------------------------------------
echo "🔑 Logging into MediaWiki as '${MW_ADMIN_USER}'..."

LOGIN_STATUS=""
for attempt in 1 2 3; do
    # Fresh cookie jar on each attempt
    : > "$COOKIE_JAR"

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
    [ "$LOGIN_STATUS" = "Success" ] && break
    [ "$attempt" -lt 3 ] && echo "  ⚠️  Login attempt ${attempt} failed, retrying..." && sleep 2
done

if [ "$LOGIN_STATUS" != "Success" ]; then
    echo "❌ Login failed after 3 attempts: $LOGIN_RESULT"
    exit 1
fi
echo "  ✅ Logged in (${MW_TARGET})"

CSRF_TOKEN=$(curl -s -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
    "${API_URL}?action=query&meta=tokens&format=json" \
    | jq -r '.query.tokens.csrftoken')

# Login to local instance too (if cleaning both)
LOCAL_API_URL="${LOCAL_URL}/api.php"
LOCAL_CSRF_TOKEN=""
if [ "$ALSO_LOCAL" -eq 1 ]; then
    LOCAL_LOGIN_TOKEN=$(curl -s -b "$COOKIE_JAR_LOCAL" -c "$COOKIE_JAR_LOCAL" \
        "${LOCAL_API_URL}?action=query&meta=tokens&type=login&format=json" \
        | jq -r '.query.tokens.logintoken')

    LOCAL_LOGIN_RESULT=$(curl -s -b "$COOKIE_JAR_LOCAL" -c "$COOKIE_JAR_LOCAL" \
        -X POST "$LOCAL_API_URL" \
        --data-urlencode "action=login" \
        --data-urlencode "lgname=${MW_ADMIN_USER}" \
        --data-urlencode "lgpassword=${MW_ADMIN_PASSWORD}" \
        --data-urlencode "lgtoken=${LOCAL_LOGIN_TOKEN}" \
        --data-urlencode "format=json")

    LOCAL_LOGIN_STATUS=$(echo "$LOCAL_LOGIN_RESULT" | jq -r '.login.result')
    if [ "$LOCAL_LOGIN_STATUS" != "Success" ]; then
        echo "  ⚠️  Local login failed — continuing with remote only"
        ALSO_LOCAL=0
    else
        LOCAL_CSRF_TOKEN=$(curl -s -b "$COOKIE_JAR_LOCAL" -c "$COOKIE_JAR_LOCAL" \
            "${LOCAL_API_URL}?action=query&meta=tokens&format=json" \
            | jq -r '.query.tokens.csrftoken')
        echo "  ✅ Logged in (local)"
    fi
fi

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
# Optional: local instance params (args 6-8)
local_cookie_jar="${6:-}"; local_api_url="${7:-}"; local_csrf_token="${8:-}"

_do_delete() {
    local cj="$1" url="$2" tok="$3"
    curl -s -b "$cj" \
        -X POST "$url" \
        --data-urlencode "action=delete" \
        --data-urlencode "title=${title}" \
        --data-urlencode "reason=Cleanup via clean.sh" \
        --data-urlencode "token=${tok}" \
        --data-urlencode "format=json" > /dev/null
}

_do_delete "$cookie_jar" "$api_url" "$csrf_token"

# Also delete from local instance if requested
if [ -n "$local_api_url" ]; then
    _do_delete "$local_cookie_jar" "$local_api_url" "$local_csrf_token" 2>/dev/null || true
fi

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

    # Build local args (empty if not cleaning both)
    local local_args=()
    if [ "$ALSO_LOCAL" -eq 1 ]; then
        local_args=("$COOKIE_JAR_LOCAL" "$LOCAL_API_URL" "$LOCAL_CSRF_TOKEN")
    fi

    # Clean progress dir
    rm -f "$PROGRESS_DIR"/*.done 2>/dev/null
    draw_progress 0 "$total" "$label"

    printf '%s\n' "$@" \
        | xargs -P "$PARALLEL_JOBS" -I{} \
            bash "$DELETE_SCRIPT" {} "$COOKIE_JAR" "$API_URL" "$CSRF_TOKEN" "$PROGRESS_DIR" ${local_args[@]+"${local_args[@]}"} &
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
echo "  🌐 ${MW_TARGET} is now empty"
if [ "$ALSO_LOCAL" -eq 1 ]; then
echo "  🐳 Local (${LOCAL_URL}) also cleaned"
fi
echo "========================================="
