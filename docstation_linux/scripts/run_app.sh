#!/usr/bin/env bash
# Запуск нативного GTK-приложения.
# После "pip install -e ." лучше использовать просто: docstation-app
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export DOCSTATION_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ "$DOCSTATION_DIR" == *"/.local/share/Trash/"* ]]; then
  echo "ERROR: запуск из Корзины" >&2; exit 2
fi

exec docstation-app
