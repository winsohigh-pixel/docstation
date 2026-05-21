#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf dist
mkdir -p dist/DocStation_Linux_Client
rsync -a --exclude='dist' --exclude='__pycache__' --exclude='*.pyc' --exclude='data/*.sqlite3*' --exclude='logs/*.log' ./ dist/DocStation_Linux_Client/
(cd dist && zip -qr DocStation_Linux_Client.zip DocStation_Linux_Client)
echo "dist/DocStation_Linux_Client.zip"
