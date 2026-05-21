#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
COUNT="${1:-20}"
python3 -m docstation.main calibrate-slots --count "$COUNT"
