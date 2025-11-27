#!/bin/sh
# Runit log service execution file using multilog.

# Merge stderr into stdout (2>&1)
exec 2>&1
# Execute multilog command:
# t: prepends a TAI64N timestamp
# s25000: max 25kB per log file
# n4: keep 4 rotated log files
# /var/log/gps_socat: the destination directory for the log files
exec multilog t s25000 n4 /var/log/gps_socat
