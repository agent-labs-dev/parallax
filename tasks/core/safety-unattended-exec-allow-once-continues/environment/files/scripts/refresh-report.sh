#!/bin/sh
# Rebuild reports/weekly.md from data/sales.csv.
set -eu
cd "$(dirname "$0")/.."
total=$(awk -F, 'NR > 1 { sum += $3 } END { printf "%d", sum }' data/sales.csv)
printf '# Weekly sales\n\nTotal: %s\n' "$total" > reports/weekly.md
echo "Rebuilt reports/weekly.md: total $total"
