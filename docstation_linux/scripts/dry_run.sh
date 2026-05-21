#!/usr/bin/env bash
set -euo pipefail
export DOCSTATION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec docstation-dryrun
