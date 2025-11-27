#!/bin/sh
# Runit log service execution file using multilog.

# Configuration for log output directory
LOG_DIR="/var/log/gps_socat"

# Merge stderr into stdout (2>&1)
exec 2>&1
# Execute multilog command:
# t: prepends a TAI64N timestamp
# s25000: max 25kB per log file
# n4: keep 4 rotated log files
# $LOG_DIR: the destination directory for the log files
exec multilog t s25000 n4 $LOG_DIR
