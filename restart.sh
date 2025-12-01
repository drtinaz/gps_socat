#!/bin/bash
# --- Configuration ---
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
SERVICE_NAME=$(basename $SCRIPT_DIR)

echo
echo "Initiating **Simple Force-Restart** for $SERVICE_NAME..."
echo "---"

## 1. RESET LOGGING STREAM (Safely using SIGALRM)
echo "Resetting log stream to capture new startup..."

# Find the PID of the multilog process for the service
# We need to find this BEFORE we kill it in the next step.
MULTILOG_PID=$(ps | grep 'multilog.*'"$SERVICE_NAME" | grep -v 'grep' | awk '{print $1}')

if [ -n "$MULTILOG_PID" ]; then
    # Sending SIGALRM to the current logger ensures the log file is rotated/cleared correctly.
    kill -ALRM $MULTILOG_PID
    echo "Log reset signal sent to PID ($MULTILOG_PID)."
else
    echo "❌ **Warning:** Could not find multilog process. Log file will not be cleared."
fi
echo "---"

## 2. KILL ALL SERVICE-RELATED PROCESSES
echo "Killing all components (App, Supervisor, Logger) to force system restart..."

# Look for PIDs related to the main service, plus the explicit child processes.
# This ensures we get all of: supervise, multilog, python (gps_socat.py), socat, and gps_dbus
PIDS_TO_KILL=$(
    ps | grep "$SERVICE_NAME" | grep -v 'grep' | grep -v "$0" | awk '{print $1}';
    ps | grep 'socat .*pty,link=.*' | grep -v 'grep' | awk '{print $1}';
    ps | grep 'gps_dbus' | grep -v 'grep' | awk '{print $1}';
)

PIDS_TO_KILL_UNIQUE=$(echo "$PIDS_TO_KILL" | sort -u | tr '\n' ' ')

if [ -z "$PIDS_TO_KILL_UNIQUE" ]; then
    echo "No running PIDs found."
else
    echo "Found PIDs: ($PIDS_TO_KILL_UNIQUE). Sending **kill -9** to all..."
    
    # KILL COMMAND (This is the action that triggers the system's immediate restart)
    kill -9 $PIDS_TO_KILL_UNIQUE 2>/dev/null
    
    # Pause briefly for the OS to finalize the kill and for svscan to react.
    sleep 1
    
    echo "All old components terminated."
fi
echo "---"

## 3. SYSTEM RESTARTS AUTOMATICALLY
echo "Service is being restarted automatically by the system scanner (svscan)."
echo "Waiting 2 seconds for new service to stabilize..."
sleep 2

echo "**Restart complete.** Check the log for the new startup messages."
