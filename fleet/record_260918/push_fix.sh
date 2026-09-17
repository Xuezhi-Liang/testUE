#!/bin/bash
# Push freeze_coverage.py (PlayerStart fallback) + start stubs (from validated routes) to every booted worker, once each.
cd /home/ubuntu/ue_record_fleet_20260918
for round in $(seq 1 40); do
  aws ec2 describe-instances --region eu-north-1 --filters Name=tag:RecordRun,Values=260918 Name=instance-state-name,Values=running --query 'Reservations[].Instances[].[Tags[?Key==`RecordWorker`].Value|[0],PrivateIpAddress]' --output text | sort > workers.txt
  pending=0
  while read w ip; do
    grep -qx "$w" fix_pushed.txt 2>/dev/null && continue
    r=$(ssh -n -o StrictHostKeyChecking=no -o ConnectTimeout=6 ubuntu@$ip '[ -f /home/ubuntu/ue_record_fleet_20260918/record_loop.log ] && echo booted || echo booting' 2>/dev/null)
    if [ "$r" = booted ]; then
      scp -o StrictHostKeyChecking=no -o ConnectTimeout=6 -q bundle/pipeline/freeze_coverage.py ubuntu@$ip:/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline/freeze_coverage.py && scp -o StrictHostKeyChecking=no -o ConnectTimeout=6 -q bundle/pipeline/longvideo/driver.sh ubuntu@$ip:/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline/longvideo/driver.sh && scp -o StrictHostKeyChecking=no -o ConnectTimeout=6 -q bundle/starts/*.json ubuntu@$ip:/home/ubuntu/WM-Unreal-data-collection/batch_inference/start_positions/ && echo "$w" >> fix_pushed.txt && echo "$(date -u +%H:%M) fix pushed to $w"
    else pending=$((pending+1)); fi
  done < workers.txt
  n=$(wc -l < workers.txt); done_n=$(wc -l < fix_pushed.txt 2>/dev/null || echo 0)
  echo "$(date -u +%H:%M) pushed $done_n of $n running, $pending booting"
  [ "$pending" -eq 0 ] && [ "$done_n" -ge 30 ] && exit 0
  sleep 120
done
