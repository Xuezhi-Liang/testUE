#!/bin/bash
cd /home/ubuntu/ue_record_fleet_20260918
todo="$(seq 2 29)"
for round in $(seq 1 40); do
  left=""
  for i in $todo; do
    if [ -f launch_$(printf %02d $i).json ]; then continue; fi
    out=$(/opt/pytorch/bin/python launch.py $i 2>&1 | tail -1); echo "$(date -u +%H:%M) $out"
    case "$out" in STARTED*) ;; *) left="$left $i";; esac
  done
  todo="$left"; [ -z "$todo" ] && { echo "all launched"; exit 0; }
  echo "$(date -u +%H:%M) waiting for capacity for:$todo"; sleep 180
done
echo "gave up after 40 rounds; still missing:$todo"
