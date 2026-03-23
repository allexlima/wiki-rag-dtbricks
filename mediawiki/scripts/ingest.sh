#!/usr/bin/env bash
# ============================================================
# 📥 Ingest a dataset into MediaWiki
# ============================================================
#
# Shared ingestion script — reads markdown files from a dataset
# directory, converts them to wikitext, and uploads pages + images
# to the MediaWiki instance via the API.
#
# Usage: ./ingest.sh <dataset_dir>
#   e.g. ./ingest.sh astromotores
#        ./ingest.sh customer
#
# Prerequisites:
#   - MediaWiki container running (run setup.sh first)
#   - mediawiki/.env with MW_ADMIN_USER and MW_ADMIN_PASSWORD
#   - jq, curl, sed installed
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DOCKER_DIR="$SCRIPT_DIR/.."
DATASET_BASE="$DOCKER_DIR/dataset"

# -------------------------------------------------------
# 0. Validate arguments & dependencies
# -------------------------------------------------------
if [ $# -lt 1 ]; then
    echo "❌ Usage: $0 <dataset_dir>"
    echo "   Example: $0 astromotores"
    exit 1
fi

DATASET_DIR="$DATASET_BASE/$1"
DATASET_NAME="$1"
IMAGES_DIR="$DATASET_DIR/images"
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
    printf "📦 Also upload to local Docker (%s)? [Y/n] " "$LOCAL_URL"
    read -r BOTH
    if [ "$BOTH" != "n" ] && [ "$BOTH" != "N" ]; then
        if curl -s --max-time 3 "${LOCAL_URL}/api.php?action=query&meta=siteinfo&format=json" >/dev/null 2>&1; then
            ALSO_LOCAL=1
            echo "   → Uploading to both remote and local"
        else
            echo "   ⚠️  Local Docker not reachable — uploading to remote only"
        fi
    else
        echo "   → Uploading to remote only"
    fi
fi

for cmd in curl jq sed; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "❌ Required command '$cmd' not found"
        exit 1
    fi
done

if [ ! -d "$DATASET_DIR" ]; then
    echo "❌ Dataset directory not found: $DATASET_DIR"
    exit 1
fi

# -------------------------------------------------------
# 1. Load .env for admin credentials
# -------------------------------------------------------
ENV_FILE="$DOCKER_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "❌ mediawiki/.env not found — need MW_ADMIN_USER and MW_ADMIN_PASSWORD"
    exit 1
fi

set -a
source "$ENV_FILE"
set +a

: "${MW_ADMIN_USER:?MW_ADMIN_USER not set in .env}"
: "${MW_ADMIN_PASSWORD:?MW_ADMIN_PASSWORD not set in .env}"

# -------------------------------------------------------
# 2. Login to MediaWiki API (with retry — ECS sessions can be flaky after scale-up)
# -------------------------------------------------------
echo "🔑 Logging into MediaWiki as '${MW_ADMIN_USER}'..."

LOGIN_STATUS=""
for attempt in 1 2 3; do
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
echo "  ✅ Logged in (remote)"

CSRF_TOKEN=$(curl -s -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
    "${API_URL}?action=query&meta=tokens&format=json" \
    | jq -r '.query.tokens.csrftoken')

# Login to local instance too (if uploading to both)
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
# 3. Convert markdown to wikitext
# -------------------------------------------------------
# md_to_wikitext is no longer used directly — conversion happens
# via AWK_PROG + SED_PROG in the parallel page upload script.

# -------------------------------------------------------
# 4. Shared infrastructure: parallel runner + progress bar
# -------------------------------------------------------
echo ""
echo "📥 Ingesting dataset: ${DATASET_NAME}"

PARALLEL_JOBS=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
PROGRESS_DIR=$(mktemp -d)
shopt -s nullglob

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

# Self-contained page upload script
PAGE_SCRIPT=$(mktemp)
cat > "$PAGE_SCRIPT" << 'PAGE_EOF'
#!/usr/bin/env bash
md_file="$1"; cookie_jar="$2"; api_url="$3"; dataset_name="$4"
csrf_token="$5"; progress_dir="$6"; md_to_wikitext_awk="$7"; md_to_wikitext_sed="$8"
# Optional: local instance params (args 9-11)
local_cookie_jar="${9:-}"; local_api_url="${10:-}"; local_csrf_token="${11:-}"

title=$(head -1 "$md_file" | sed 's/^# //')
[ -z "$title" ] && title=$(basename "$md_file" .md | tr '_' ' ')

# Main_Page: always overwrite (MediaWiki creates a default one on install)
base=$(basename "$md_file" .md)
is_main_page=0
[ "$base" = "Main_Page" ] && is_main_page=1

# Skip non-Main_Page pages that already exist
if [ "$is_main_page" -eq 0 ]; then
    exists=$(curl -s -b "$cookie_jar" \
        "${api_url}?action=query&titles=$(printf '%s' "$title" | jq -sRr @uri)&format=json" \
        | jq -r '.query.pages | to_entries[0].value.missing // "exists"')
    if [ "$exists" = "exists" ]; then
        touch "${progress_dir}/$(basename "$md_file").skip"
        exit 0
    fi
fi

content=$(awk "$md_to_wikitext_awk" "$md_file" | sed "$md_to_wikitext_sed")

_upload_page() {
    local cj="$1" url="$2" tok="$3"
    curl -s -b "$cj" \
        -X POST "$url" \
        --data-urlencode "action=edit" \
        --data-urlencode "title=${title}" \
        --data-urlencode "text=${content}" \
        --data-urlencode "summary=Import from dataset/${dataset_name}" \
        --data-urlencode "token=${tok}" \
        --data-urlencode "format=json"
}

result=$(_upload_page "$cookie_jar" "$api_url" "$csrf_token")
status=$(echo "$result" | jq -r '.edit.result // .error.code // "unknown"')

# Also upload to local instance if requested
if [ -n "$local_api_url" ]; then
    _upload_page "$local_cookie_jar" "$local_api_url" "$local_csrf_token" >/dev/null 2>&1
fi

touch "${progress_dir}/$(basename "$md_file").done"
if [ "$status" != "Success" ]; then
    echo "${title}: ${status}" >> "${progress_dir}/page_errors.log"
fi
PAGE_EOF
chmod +x "$PAGE_SCRIPT"

# Self-contained image upload script
UPLOAD_SCRIPT=$(mktemp)
cat > "$UPLOAD_SCRIPT" << 'UPLOAD_EOF'
#!/usr/bin/env bash
img_file="$1"; cookie_jar="$2"; api_url="$3"; dataset_name="$4"
csrf_token="$5"; progress_dir="$6"
# Optional: local instance params (args 7-9)
local_cookie_jar="${7:-}"; local_api_url="${8:-}"; local_csrf_token="${9:-}"

filename=$(basename "$img_file")

_upload_image() {
    local cj="$1" url="$2" tok="$3"
    local max_retries=3
    for attempt in $(seq 1 $max_retries); do
        result=$(curl -s -b "$cj" \
            -X POST "$url" \
            -F "action=upload" \
            -F "filename=${filename}" \
            -F "file=@${img_file}" \
            -F "comment=Import from dataset/${dataset_name}" \
            -F "token=${tok}" \
            -F "ignorewarnings=true" \
            -F "format=json")
        st=$(echo "$result" | perl -pe 's/[^\x20-\x7E\x0A\x0D]//g' | jq -r '.upload.result // .error.code // "unknown"')
        if [ "$st" = "Success" ] || [ "$st" = "Warning" ]; then
            echo "$st"; return
        fi
        [ "$attempt" -lt "$max_retries" ] && sleep 1
    done
    echo "$st"
}

# Skip if file already exists on remote
exists=$(curl -s -b "$cookie_jar" \
    "${api_url}?action=query&titles=File:${filename}&format=json" \
    | jq -r '.query.pages | to_entries[0].value.missing // "exists"')
if [ "$exists" = "exists" ]; then
    # Still upload to local if needed (may not exist there)
    if [ -n "$local_api_url" ]; then
        _upload_image "$local_cookie_jar" "$local_api_url" "$local_csrf_token" >/dev/null 2>&1
    fi
    touch "${progress_dir}/${filename}.done"
    exit 0
fi

status=$(_upload_image "$cookie_jar" "$api_url" "$csrf_token")

# Also upload to local instance if requested
if [ -n "$local_api_url" ]; then
    _upload_image "$local_cookie_jar" "$local_api_url" "$local_csrf_token" >/dev/null 2>&1
fi

touch "${progress_dir}/${filename}.done"
if [ "$status" != "Success" ] && [ "$status" != "Warning" ]; then
    echo "$filename: $status" >> "${progress_dir}/img_errors.log"
fi
UPLOAD_EOF
chmod +x "$UPLOAD_SCRIPT"

# Extract awk and sed programs as strings to pass to subprocesses
AWK_PROG='
# Skip the first # heading (MediaWiki uses page title from API, not content)
NR == 1 && /^# / { next }
# Skip blank line right after removed heading
NR == 2 && /^$/ { next }
# Convert markdown tables to wikitable
/^\|/ {
    if ($0 ~ /^\| *:?-+/) { next }
    line = $0; gsub(/^ *\| */, "", line); gsub(/ *\| *$/, "", line)
    n = split(line, cells, / *\| */)
    if (!in_table) { print "{| class=\"wikitable\""; in_table = 1; print "|-"
        for (i = 1; i <= n; i++) print "! " cells[i]; next }
    print "|-"; for (i = 1; i <= n; i++) print "| " cells[i]; next }
{ if (in_table) { print "|}"; in_table = 0 } print }
END { if (in_table) print "|}" }'

SED_PROG='s/^### /=== /; s/ *$//
s/^## /== /; s/ *$//
s/^# /= /; s/ *$//
/^= [^|]/s/$/ =/
/^== [^|]/s/$/ ==/
/^=== [^|]/s/$/ ===/
s/\*\*\([^*]*\)\*\*/\x27\x27\x27\1\x27\x27\x27/g
s/\*\([^*]*\)\*/\x27\x27\1\x27\x27/g
s/^- /* /
s/^  - /** /
s/^    - /*** /
s/^> \(.*\)/<blockquote>\1<\/blockquote>/
s/!\[\([^]]*\)\](images\/\([^)]*\))/[[File:\2|\1]]/
s/!\[\([^]]*\)\](\([^)]*\))/[[File:\2|\1]]/
s/\[\([^]]*\)\](\([^)]*\))/[[\2|\1]]/g
s/^---$/----/'

# -------------------------------------------------------
# 5. Create wiki pages (parallel + progress bar)
# -------------------------------------------------------
MD_FILES=()
for md_file in "$DATASET_DIR"/*.md; do
    [ -f "$md_file" ] || continue
    MD_FILES+=("$md_file")
done
TOTAL_PAGES=${#MD_FILES[@]}

if [ "$TOTAL_PAGES" -gt 0 ]; then
    rm -f "$PROGRESS_DIR"/*.done 2>/dev/null
    draw_progress 0 "$TOTAL_PAGES" "📄 Pages "

    # Build local args (empty array if not uploading to both)
    LOCAL_ARGS=()
    if [ "$ALSO_LOCAL" -eq 1 ]; then
        LOCAL_ARGS=("$COOKIE_JAR_LOCAL" "$LOCAL_API_URL" "$LOCAL_CSRF_TOKEN")
    fi

    printf '%s\n' "${MD_FILES[@]}" \
        | xargs -P "$PARALLEL_JOBS" -I{} \
            bash "$PAGE_SCRIPT" {} "$COOKIE_JAR" "$API_URL" "$DATASET_NAME" "$CSRF_TOKEN" "$PROGRESS_DIR" "$AWK_PROG" "$SED_PROG" ${LOCAL_ARGS[@]+"${LOCAL_ARGS[@]}"} &
    PID=$!

    while kill -0 "$PID" 2>/dev/null; do
        DONE=$(find "$PROGRESS_DIR" -name "*.done" -o -name "*.skip" 2>/dev/null | wc -l | tr -d ' ')
        draw_progress "$DONE" "$TOTAL_PAGES" "📄 Pages "
        sleep 0.5
    done
    wait "$PID" || true
    draw_progress "$TOTAL_PAGES" "$TOTAL_PAGES" "📄 Pages "
    SKIPPED_PAGES=$(find "$PROGRESS_DIR" -name "*.skip" 2>/dev/null | wc -l | tr -d ' ')
    [ "$SKIPPED_PAGES" -gt 0 ] && printf " (%d skipped)" "$SKIPPED_PAGES"
    echo ""

    if [ -f "${PROGRESS_DIR}/page_errors.log" ]; then
        echo "  ⚠️  Some pages failed:"
        while IFS= read -r line; do echo "    ⚠️  $line"; done < "${PROGRESS_DIR}/page_errors.log"
    fi
fi

# -------------------------------------------------------
# 6. Upload images (parallel + progress bar)
#    Cap at 4 to avoid MediaWiki file lock contention
# -------------------------------------------------------
IMG_JOBS=$((PARALLEL_JOBS > 4 ? 4 : PARALLEL_JOBS))
IMAGE_COUNT=0
rm -f "$PROGRESS_DIR"/*.done "$PROGRESS_DIR"/*.skip 2>/dev/null

if [ -d "$IMAGES_DIR" ]; then
    IMAGE_FILES=()
    for img_file in "$IMAGES_DIR"/*.jpg "$IMAGES_DIR"/*.jpeg "$IMAGES_DIR"/*.png "$IMAGES_DIR"/*.gif "$IMAGES_DIR"/*.svg; do
        [ -f "$img_file" ] || continue
        IMAGE_FILES+=("$img_file")
    done

    TOTAL_IMAGES=${#IMAGE_FILES[@]}
    if [ "$TOTAL_IMAGES" -gt 0 ]; then
        rm -f "$PROGRESS_DIR"/*.done 2>/dev/null
        draw_progress 0 "$TOTAL_IMAGES" "🖼️ Images"

        # Build local args for images
        LOCAL_IMG_ARGS=()
        if [ "$ALSO_LOCAL" -eq 1 ]; then
            LOCAL_IMG_ARGS=("$COOKIE_JAR_LOCAL" "$LOCAL_API_URL" "$LOCAL_CSRF_TOKEN")
        fi

        printf '%s\n' "${IMAGE_FILES[@]}" \
            | xargs -P "$IMG_JOBS" -I{} \
                bash "$UPLOAD_SCRIPT" {} "$COOKIE_JAR" "$API_URL" "$DATASET_NAME" "$CSRF_TOKEN" "$PROGRESS_DIR" ${LOCAL_IMG_ARGS[@]+"${LOCAL_IMG_ARGS[@]}"} &
        PID=$!

        while kill -0 "$PID" 2>/dev/null; do
            DONE=$(find "$PROGRESS_DIR" -name "*.done" -o -name "*.skip" 2>/dev/null | wc -l | tr -d ' ')
            draw_progress "$DONE" "$TOTAL_IMAGES" "🖼️ Images"
            sleep 0.5
        done
        wait "$PID" || true
        draw_progress "$TOTAL_IMAGES" "$TOTAL_IMAGES" "🖼️ Images"
        echo ""
        IMAGE_COUNT=$TOTAL_IMAGES

        if [ -f "${PROGRESS_DIR}/img_errors.log" ]; then
            echo "  ⚠️  Some uploads failed:"
            while IFS= read -r line; do echo "    ⚠️  $line"; done < "${PROGRESS_DIR}/img_errors.log"
        fi
    fi
fi
rm -rf "$PROGRESS_DIR" "$PAGE_SCRIPT" "$UPLOAD_SCRIPT"

# -------------------------------------------------------
# Done
# -------------------------------------------------------
echo ""
echo "========================================="
echo "  ✅ Ingested ${TOTAL_PAGES} pages + ${IMAGE_COUNT} images"
echo "  📂 Dataset: ${DATASET_NAME}"
echo "  🌐 View at ${MW_TARGET}"
if [ "$ALSO_LOCAL" -eq 1 ]; then
echo "  🐳 Also synced to ${LOCAL_URL}"
fi
echo "========================================="
