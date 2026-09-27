#!/bin/bash
# The remote Screddy Hermes cron checks the local Mac's published snapshot.
set -euo pipefail
export HOME=/home/hermes
export PATH=/home/hermes/.local/bin:/usr/local/bin:/usr/bin:/bin
export SAVINGS_STATE_DIR=/home/hermes/.hermes/savings
/usr/bin/python3 /home/hermes/.hermes/scripts/weekly-savings-delivery.py dispatch
