#!/usr/bin/env bash
# ============================================================
# 📂 Interactive Dataset Selector
# ============================================================
#
# Scans for dataset subdirectories (folders containing *.md files),
# presents a numbered list, and runs ingest.sh on the selected one.
#
# Usage: ./select_dataset.sh
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATASET_BASE="$SCRIPT_DIR/../dataset"
cd "$DATASET_BASE"

# -------------------------------------------------------
# 1. Discover available datasets
# -------------------------------------------------------
DATASETS=()
for dir in */; do
    dir="${dir%/}"
    [ -d "$dir" ] || continue
    ls "$dir"/*.md &>/dev/null || continue
    DATASETS+=("$dir")
done

if [ ${#DATASETS[@]} -eq 0 ]; then
    echo "❌ No datasets found (looking for directories with *.md files)"
    exit 1
fi

# -------------------------------------------------------
# 2. Auto-select if only one dataset
# -------------------------------------------------------
if [ ${#DATASETS[@]} -eq 1 ]; then
    echo "📂 Only one dataset found: ${DATASETS[0]}"
    exec "$SCRIPT_DIR/ingest.sh" "${DATASETS[0]}"
fi

# -------------------------------------------------------
# 3. Numbered menu
# -------------------------------------------------------
echo ""
echo "  📂 Available datasets:"
echo ""
for i in "${!DATASETS[@]}"; do
    n=$((i + 1))
    md_count=$(ls "${DATASETS[$i]}"/*.md 2>/dev/null | wc -l | tr -d ' ')
    printf "  \033[36m%d)\033[0m %s (%s pages)\n" "$n" "${DATASETS[$i]}" "$md_count"
done
echo ""

while true; do
    printf "  Enter number [1-%d]: " "${#DATASETS[@]}"
    read -rsn1 choice

    if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#DATASETS[@]}" ]; then
        selected=$((choice - 1))
        echo "$choice"
        break
    fi
    echo ""
    echo "  ⚠️  Invalid choice. Try again."
done

echo ""
echo "  ✅ Selected: ${DATASETS[$selected]}"
echo ""

exec "$SCRIPT_DIR/ingest.sh" "${DATASETS[$selected]}"
