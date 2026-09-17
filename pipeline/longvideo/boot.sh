#!/usr/bin/env bash
# Long-video shard boot entry, invoked by EC2 user-data as user ubuntu. Gated on the LongVideoShard
# tag. Flow: tags -> hotfix -> container -> record -> upload -> stop instance.
set -u
exec >> /home/ubuntu/longvideo_boot.log 2>&1
echo "==== boot $(date) ===="
P=/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline
L=$P/longvideo
OPS=s3://pan-simworld/ue-revist-long-video/_ops

TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
IID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
get_tag() { aws ec2 describe-tags --region "$REGION" \
  --filters "Name=resource-id,Values=$IID" "Name=key,Values=$1" \
  --query 'Tags[0].Value' --output text 2>/dev/null; }
SHARD_ID=$(get_tag LongVideoShard)
NAVB=$(get_tag LongVideoNavExport); case "$NAVB" in None|"") NAVB="";; esac
if [ -z "$SHARD_ID" ] || [ "$SHARD_ID" = "None" ]; then
  # A nav-export instance carries no shard: it records nothing, so it needs no assignment.
  if [ -z "$NAVB" ]; then echo "no LongVideoShard tag"; exit 0; fi
  SHARD_ID="nav$NAVB"
fi
MAP_ID=$(get_tag LongVideoMapId)
BUDGET_H=$(get_tag LongVideoBudgetHours); case "$BUDGET_H" in None|"") BUDGET_H=7;; esac
DEADLINE_EPOCH=$(( $(date +%s) + BUDGET_H * 3600 ))
echo "assignment: $SHARD_ID  $MAP_ID  budget ${BUDGET_H}h  deadline $(date -d @$DEADLINE_EPOCH)"
rm -f "$L/stop_uploads_$SHARD_ID"

# The AMI is ue-campaign-20260818-1215; everything the long-video work added is newer, so the
# pipeline is replaced wholesale from S3 rather than by re-baking a 4 TB image. episodes/ and
# frozen/ are NOT in the tarball - the clone makes its own.
if aws s3 cp "$OPS/pipeline.tar.gz" /tmp/pipeline.tar.gz --only-show-errors; then
  tar xzf /tmp/pipeline.tar.gz -C "$P" && echo "hotfix pipeline applied"
else
  echo "FATAL: no hotfix tarball at $OPS/pipeline.tar.gz; the AMI's pipeline predates this job"
  exit 1
fi

for i in $(seq 1 30); do nvidia-smi -L >/dev/null 2>&1 && break; sleep 10; done

# The C++ capture module ships as source in the tarball and the AMI's compiled copy predates it
# (manual exposure, the RGB probe). Install + incremental build inside the container before the
# first editor launch: 16 s when only these files changed, and a build failure is a boot failure
# rather than an editor that silently runs last month's actor. Done after the container is up.
BUILD_MODULE=1

nohup bash /home/ubuntu/prj/SimWorld/launch_unreal_instance/start_enroot_container.sh \
  "exec sleep 999999999" > /home/ubuntu/container_boot.log 2>&1 &
# The parking pid must be the CONTAINERIZED sleep (comm=sleep, enroot seccomp filter present),
# not the host-side wrapper whose cmdline also contains the marker string.
CTR_PID=""
for i in $(seq 1 90); do
  for cand in $(pgrep -f "sleep 999999999"); do
    if [ "$(cat /proc/$cand/comm 2>/dev/null)" = "sleep" ] && \
       grep -q "^Seccomp:.*2" /proc/$cand/status 2>/dev/null; then
      CTR_PID=$cand; break
    fi
  done
  [ -n "$CTR_PID" ] && break
  sleep 5
done
if [ -z "$CTR_PID" ]; then echo "container failed to start"; exit 1; fi
export CTR_PID
echo "container pid $CTR_PID"
sleep 10
if [ "${BUILD_MODULE:-0}" = 1 ]; then
  echo "==== $(date -u +%H:%M:%S) building gym_citynavRuntime from $P/cpp ===="
  enroot exec "$CTR_PID" bash -lc "cd $P/cpp && python3 install.py && cd /home/ue4/simworld && \
    /home/ue4/UnrealEngine/Engine/Build/BatchFiles/Linux/Build.sh gym_citynavEditor Linux Development \
    -project=/home/ue4/simworld/gym_citynav.uproject -waitmutex" > /home/ubuntu/module_build.log 2>&1
  if grep -q "Result: Succeeded" /home/ubuntu/module_build.log; then
    echo "module build OK: $(grep -m1 'Total execution time' /home/ubuntu/module_build.log)"
  else
    echo "FATAL: module build failed; see module_build.log"; tail -30 /home/ubuntu/module_build.log
    aws s3 cp /home/ubuntu/module_build.log "$OPS/module_build_failed_$(hostname).log" 2>/dev/null
    exit 1
  fi
fi

# NAV EXPORT MODE: export the navmesh of every map in this instance's batch and upload the
# .bin/.json pairs. No recording. The tag value is an index into longvideo/nav_export_batches.json
# (shipped in the tarball); each map gets its own editor launch via probe_once.sh, because a map
# switch in a live editor keeps the previous level's navmesh (CLAUDE.md). Exists so survey_core.py
# can rank ALL the maps that have a start-positions file, not just the 25 that had been exported
# on the dev host by earlier runs.
if [ -n "$NAVB" ]; then
  echo "NAV EXPORT MODE: batch $NAVB"
  S3N=s3://pan-simworld/ue-revist-long-video/_nav
  # Per-map cap, seconds. Overridable per instance because a big city level on a fresh instance
  # (no DDC) can spend the whole default compiling shaders and still be healthy.
  NAV_CAP_S=$(get_tag LongVideoNavCap); case "$NAV_CAP_S" in None|"") NAV_CAP_S=1500;; esac
  MAPS=$(python3 -c "
import json; b=json.load(open('$L/nav_export_batches.json'))
for m in b[str($NAVB)]: print(m['slug'] + '|' + m['map_id'])")
  for pair in $MAPS; do
    SLUG=${pair%%|*}; MAPID=${pair##*|}
    echo "==== $(date -u +%H:%M:%S) $SLUG ===="
    # Hard cap per map. The first fleet had four instances wedge on one map each for 2.5 h with
    # no log and none of the Python-side timeouts firing (LowPolyMedieval Map_5, ModularCourtyard
    # x2, SwimmingPool ChangingRoom_Male) - whatever it was, the batch behind that map never ran.
    # A map that cannot export in 25 min is a refusal for this survey, and it is written as one.
    CTR_PID=$CTR_PID GAME_MAP=$MAPID SLUG=$SLUG SCRIPT=$L/nav_export_only.py TAG=nav_$SLUG \
      timeout -k 60 "$NAV_CAP_S" bash "$L/probe_once.sh"
    rc=$?
    if [ $rc -eq 124 ] || [ $rc -eq 137 ]; then
      echo "NAVEXPORT {\"slug\": \"$SLUG\", \"map_id\": \"$MAPID\", \"ok\": false, \"error\": \"TimeoutError: no export after $NAV_CAP_S s (editor or script wedged); killed\"}" \
        | tee -a "$P/logs/lv_nav_${SLUG}_out.log"
      # the editor the timeout left behind holds the UnrealCV port; the next launch waits on it
      for pid in $(pgrep -f "[U]nrealEditor"); do kill -9 $pid 2>/dev/null; done; sleep 5
    fi
    grep -h "NAVEXPORT" "$P/logs/lv_nav_${SLUG}_out.log" 2>/dev/null | tail -1
    if [ -s "$P/frozen/nav/$SLUG.bin" ]; then
      aws s3 cp "$P/frozen/nav/$SLUG.bin" "$S3N/$SLUG.bin" --only-show-errors
      aws s3 cp "$P/frozen/nav/$SLUG.json" "$S3N/$SLUG.json" --only-show-errors
    fi
    aws s3 cp "$P/logs/lv_nav_${SLUG}_out.log" "$S3N/logs/$SLUG.log" --only-show-errors 2>/dev/null
  done
  aws s3 cp /home/ubuntu/longvideo_boot.log "$S3N/logs/batch_${NAVB}_boot.log" 2>/dev/null
  echo "nav export batch $NAVB complete - stopping instance"
  sudo shutdown -h +2 "nav export done"
  exit 0
fi

# PROBE MODE: run longvideo/probe_matrix.py on this shard's map and upload the table. No
# recording, no uploader. The tag value is the COMBOS list with '+' for ',' (a comma inside an
# EC2 tag value breaks the CLI's shorthand parser): 40:90+60:90+60:120 means ground clearance 40
# and 60 cm against corridor clearance 90 and 120 cm. Exists because two courtyard maps came back
# as 8-minute "100% covers" of 22-35% of their network - the 6 cm body cannot step over a door
# sill - and the right clearance is a measurement, not a guess.
PROBE=$(get_tag LongVideoProbe); case "$PROBE" in None|"") PROBE="";; esac
if [ -n "$PROBE" ]; then
  COMBOS=$(echo "$PROBE" | tr '+' ',')
  echo "PROBE MODE: probe_matrix on $MAP_ID  COMBOS=$COMBOS"
  CTR_PID=$CTR_PID GAME_MAP=$MAP_ID SLUG=$SHARD_ID SCRIPT=$L/probe_matrix.py \
    TAG=probe_$SHARD_ID COMBOS=$COMBOS bash "$L/probe_once.sh"
  S=s3://pan-simworld/ue-revist-long-video/_status
  aws s3 cp "$P/logs/lv_probe_${SHARD_ID}_out.log" "$S/${SHARD_ID}_probe_matrix.log" 2>/dev/null
  aws s3 cp /home/ubuntu/longvideo_boot.log "$S/${SHARD_ID}_probe_boot.log" 2>/dev/null
  echo "probe complete - stopping instance"
  sudo shutdown -h +2 "probe done"
  exit 0
fi

SHARD_ID=$SHARD_ID bash "$L/uploader.sh" & UPL=$!

# RECOVERY MODE. With the LongVideoRecover tag set, this instance does not record: it packages the
# frames a killed capture already left on disk and uploads them. The engine writes every frame as
# it goes and capture_engine writes the metadata only at the end, so an interrupted run leaves
# hours of real frames that nothing downstream will look at - uploader.sh waits for
# acceptance.json. Recording again would overwrite the very thing being recovered.
# RESYNC: upload only. No recording, no packaging - just put the episode on disk back through
# `aws s3 sync`, which uploads whatever is missing and skips what matches.
#
# This mode exists because a completeness check found ONE depth EXR absent from a 406,080-frame
# episode - frame 204,956, in the middle, with 201,123 frames after it. frames.csv still had its
# row, so the delivered table pointed at a file that was not there, and every downstream read
# would have needed a guard for it. There was no way to fix that without it: uploader.sh skips an
# episode already in its ledger, and recover.py skips one that already has acceptance.json. So a
# single missing object required either re-packaging four hours of frames or hand-editing the
# instance. Clearing the ledger and re-running the sync is the whole fix.
RESYNC=$(get_tag LongVideoResync)
case "$RESYNC" in
  1|true|yes)
    echo "RESYNC MODE: re-running the upload only; nothing is recorded or re-packaged"
    rm -f "$L/uploaded_${SHARD_ID}.txt"
    sleep 300      # let the uploader started above make its first pass
    for i in $(seq 1 60); do
      pgrep -f "uploader.sh" >/dev/null || break
      grep -q "uploaded" "$P/logs/lv_${SHARD_ID}_uploader.log" 2>/dev/null && break
      sleep 30
    done
    tail -5 "$P/logs/lv_${SHARD_ID}_uploader.log" 2>/dev/null
    touch "$L/stop_uploads_$SHARD_ID"
    wait $UPL 2>/dev/null
    aws s3 cp "$P/logs/lv_${SHARD_ID}_uploader.log" \
      "s3://pan-simworld/ue-revist-long-video/_status/${SHARD_ID}_resync.log" 2>/dev/null
    echo "resync complete - stopping instance"
    sudo shutdown -h +2 "resync done"
    exit 0
    ;;
esac

RECOVER=$(get_tag LongVideoRecover)
# A tag, so the choice is per-instance and visible in the console rather than baked into a script.
SKIP_GATES=$(get_tag LongVideoSkipGates); case "$SKIP_GATES" in None|"") SKIP_GATES="";; esac
[ -n "$SKIP_GATES" ] && echo "SKIP_GATES=$SKIP_GATES - packaging for delivery, gates not run"
DELIVER_PARTIAL=$(get_tag LongVideoDeliverPartial)
case "$DELIVER_PARTIAL" in None|"") DELIVER_PARTIAL="";; esac
[ -n "$DELIVER_PARTIAL" ] && echo "DELIVER_PARTIAL=$DELIVER_PARTIAL - a malformed frames.csv is delivered as its well-formed prefix"
case "$RECOVER" in
  1|true|yes)
    echo "RECOVERY MODE: packaging what is on disk, not recording"
    # Disk and filesystem facts, printed BEFORE the work. A frames.csv row came back short by its
    # last seven columns on every shard, and the same code writing 400k rows on the dev host is
    # byte-clean - so the difference is here, not in the writer, and these are the numbers that
    # would show it. Cheap, and impossible to obtain afterwards from a stopped instance.
    echo "--- disk before recovery ---"
    df -h / "$P" 2>/dev/null
    df -i / "$P" 2>/dev/null | sed 's/^/inodes: /'
    mount | grep -E " on / | $(dirname "$P") " 2>/dev/null | head -4
    du -sh "$P/episodes" 2>/dev/null | sed 's/^/episodes: /'
    echo "---"
    # UE_LOG matters here even though recovery launches no editor: the killed run's log is still
    # on disk (nothing relaunches the editor, so UE never rotates it), and it is the only place a
    # Default Material fallback is recorded. Without it `materials_compiled` is skipped and the
    # episode ships with that check unmade.
    enroot exec "$CTR_PID" bash -lc \
      "cd $P && OPENCV_IO_ENABLE_OPENEXR=1 SHARD_ID=$SHARD_ID SKIP_GATES=${SKIP_GATES:-} DELIVER_PARTIAL=${DELIVER_PARTIAL:-} \
       UE_LOG=/home/ue4/simworld/Saved/Logs/gym_citynav.log python3 -u $L/recover.py" \
      2>&1 | tee -a /home/ubuntu/longvideo_recover.log
    aws s3 cp "$P/_recover.json" \
      "s3://pan-simworld/ue-revist-long-video/_status/${SHARD_ID}_recover.json" 2>/dev/null
    ;;
  *)
    SHARD_ID=$SHARD_ID MAP_ID=$MAP_ID DEADLINE_EPOCH=$DEADLINE_EPOCH CTR_PID=$CTR_PID \
      SKIP_GATES=${SKIP_GATES:-} bash "$L/driver.sh"
    ;;
esac
echo "driver finished; final upload pass"
sleep 240
touch "$L/stop_uploads_$SHARD_ID"
wait $UPL 2>/dev/null

S=s3://pan-simworld/ue-revist-long-video/_status
aws s3 cp "$L/state_${SHARD_ID}.json" "$S/${SHARD_ID}_final_state.json" 2>/dev/null
aws s3 cp /home/ubuntu/longvideo_boot.log "$S/${SHARD_ID}_boot.log" 2>/dev/null
aws s3 cp "$P/logs/lv_${SHARD_ID}_runner.log" "$S/${SHARD_ID}_runner.log" 2>/dev/null
aws s3 cp "$P/logs/lv_${SHARD_ID}_uploader.log" "$S/${SHARD_ID}_uploader.log" 2>/dev/null
echo "shard complete - stopping instance"
sudo shutdown -h +2 "long video shard done"
