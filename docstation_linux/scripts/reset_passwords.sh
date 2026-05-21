#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
ADMIN="${1:-888}"
OPERATOR="${2:-111}"
SERVICE="${3:-7777}"
python3 -m docstation.main reset-passwords --admin "$ADMIN" --operator "$OPERATOR" --service "$SERVICE"
echo "Пароли сброшены: admin=$ADMIN operator=$OPERATOR service=$SERVICE"
