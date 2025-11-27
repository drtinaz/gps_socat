#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
SERVICE_NAME=$(basename $SCRIPT_DIR)

echo
echo "Restarting $SERVICE_NAME..."

svc -t /service/$SERVICE_NAME
echo "done."

echo