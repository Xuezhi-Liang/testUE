#!/bin/bash
cd /home/ubuntu/ue_record_fleet_20260918
pkill -f "requeue_failed.sh" ; sleep 1
nohup setsid ./requeue_failed.sh > requeue_failed.log 2>&1 < /dev/null &
sleep 2; pgrep -f "requeue_failed.sh" | wc -l
