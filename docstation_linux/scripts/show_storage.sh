#!/usr/bin/env bash
# Show current storage disk slots and their state
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m docstation.main --config StationConfig.linux.json show-storage
