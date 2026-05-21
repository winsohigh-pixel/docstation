#!/usr/bin/env bash
set -euo pipefail
sudo journalctl -kf | grep --line-buffered -Ei 'usb|uas|scsi|sd[a-z]|reset|descriptor|error|disconnect|connect'
