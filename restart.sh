#!/bin/bash
# --- Configuration ---
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
SERVICE_NAME=$(basename $SCRIPT_DIR)
SERVICE_PATH="/service/$SERVICE_NAME"
# Time in seconds to wait for graceful shutdown/check
GRACE_PERIOD=3 

echo
echo "Initiating **Fail-Safe Restart** for $SERVICE_NAME..."

## 1. STOP SERVICE & CLEAN UP (Your Robust Logic)
echo "--- Service Shutdown and Cleanup ---"

# Attempt graceful shutdown (optional, but good practice)
svc -d $SERVICE_PATH
echo "Sent shutdown command. Waiting ${GRACE_PERIOD} seconds..."
sleep $GRACE_PERIOD

# Check for remaining processes (PIDS) and force kill
PIDS=$(fuser $SERVICE_PATH 2>/dev/null)

if [ -n "$PIDS" ]; then
    echo "⚠️ **Warning:** PIDs ($PIDS) still exist. Forcing kill..."
    # The kill command is the action that triggers the supervisor to restart the service
    kill -9 $PIDS 2>/dev/null
    sleep 1 # Wait for kill -9 to take effect and for supervisor to detect the death
    
    PIDS_AFTER_KILL=$(fuser $SERVICE_PATH 2>/dev/null)
    if [ -n "$PIDS_AFTER_KILL" ]; then
        echo "❌ **Error:** Failed to kill PIDs ($PIDS_AFTER_KILL). Aborting."
        exit 1
    fi
    echo "Service cleanup complete."
else
    # Since it shut down cleanly, explicitly tell supervisor to start it
    svc -u $SERVICE_PATH
    echo "Graceful shutdown, starting service..."
fi

## 2. RESET LOGGING STREAM
echo "--- Log Rotation ---"
# Find the PID of the multilog process for this service
MULTILOG_PID=$(ps | grep 'multilog.*'"$SERVICE_NAME" | grep -v grep | awk '{print $1}')

if [ -n "$MULTILOG_PID" ]; then
    echo "Found multilog PID ($MULTILOG_PID). Sending SIGALRM to force rotation..."
    kill -ALRM $MULTILOG_PID
    echo "Log rotation signal sent."
else
    echo "❌ **Warning:** Could not find multilog process. Log rotation skipped."
fi

## 3. FINAL STAGE: SERVICE RESTARTED AUTOMATICALLY
echo "--- Service Startup ---"
# The service was either restarted by svc -u above, or automatically by 'supervise' 
# when the PIDs were killed. Wait a moment to allow the new service to stabilize.
echo "Waiting for supervise to complete automatic restart..."
sleep 2

echo "**Restart complete.**"
echo
