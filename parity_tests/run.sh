#!/usr/bin/env bash
# Run the Python ↔ Node-RED parity sweep.
#
# Prereqs (one-time):
#   - mosquitto running on localhost:1883
#   - Node-RED running with /home/jonas/Skule/noderedsmartocean/flows.json loaded
#     (i.e. `cp` it into ~/.node-red/ and restart, OR start with --userDir)
#
# Usage:
#   ./run.sh                  # all vendors, all files
#   ./run.sh -k wsense        # only WSense tests
#   ./run.sh -x                # stop after first failure
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
VENV=/home/jonas/Skule/data_transformer/.venv
cd "$HERE"
PYTHONPATH="$HERE" "$VENV/bin/pytest" -v --tb=short "$@"
