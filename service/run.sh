#!/bin/bash
# Runit service execution file for the GPS monitor.

# Path to your python script (relative to the app directory)
SERVICE_SCRIPT="/data/apps/gps_socat/gps_socat.py"

# Execute the script. 
# 'exec' ensures the Python process is PID 1 for the service manager.
# '2>&1' redirects stderr to stdout, merging all logs into the pipe 
# for multilog to capture.
exec $SERVICE_SCRIPT 2>&1
