#!/bin/bash
# Retry starting the stopped fleet workers until g6.4xlarge capacity comes back, for up to 3 h.
cd /home/ubuntu/ue_route_fleet_20260916
pending="$*"; deadline=$(( $(date +%s) + 3*3600 ))
while [ -n "$pending" ] && [ "$(date +%s)" -lt "$deadline" ]; do
  still=""
  for w in $pending; do
    out=$(/opt/pytorch/bin/python3 launch.py "$w" 2>&1 | tail -1)
    if [[ "$out" == STARTED* ]]; then echo "$(date -u +%H:%M) $out"; else still="$still $w"; fi
  done
  pending="${still# }"
  [ -n "$pending" ] && { echo "$(date -u +%H:%M) still waiting for capacity: $pending"; sleep 180; }
done
echo "$(date -u +%H:%M) done; unstarted: '$pending'"
