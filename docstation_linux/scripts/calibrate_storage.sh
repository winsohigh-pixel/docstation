#!/usr/bin/env bash
# Calibrate physical SATA storage slots (CLI mode, no GTK needed)
set -euo pipefail
cd "$(dirname "$0")/.."
COUNT="${1:-4}"
python3 -m docstation.main --config StationConfig.linux.json calibrate-storage --count "$COUNT"
