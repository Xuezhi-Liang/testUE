#!/usr/bin/env bash
# Incremental uploader for one map's long episode + status beacon.
#   SLUG=... bash uploader.sh      (stops when stop_uploads_<SLUG> appears)
#
# One episode, but the upload is still incremental and still a sync: 3.5 M objects at 0.5-0.9 TB
# does not go up in one call, and a sync that is interrupted resumes instead of restarting.
# rgb.mp4 / rgb_proxy.mp4 / rgb_keyframes are review artifacts and stay out, exactly as in the
# campaign - the delivered format is the per-frame jpg and exr.
set -u
P=/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline
L=$P/longvideo
SHARD_ID=${SHARD_ID:?}
# Map slug = shard id minus its shard suffix. The suffix is `__s07` in round one and `__r2s07` in
# a later round; `${SHARD_ID%%__s*}` handled only the first and, on `__r2s02`, matched nothing -
# so the whole shard id became the "map" and the episode landed under its own top-level prefix.
SLUG=${SLUG:-$(echo "$SHARD_ID" | sed -E 's/__r?[0-9]*s[0-9]+$//')}
LEDGER=$L/uploaded_${SHARD_ID}.txt
PENDING=0
DSTROOT=s3://pan-simworld/ue-revist-long-video/$SLUG
STATUS=s3://pan-simworld/ue-revist-long-video/_status
LOG=$P/logs/lv_${SHARD_ID}_uploader.log
touch "$LEDGER"
while true; do
  for d in "$P"/episodes/*coverage_walk*/; do
    [ -d "$d" ] || continue
    ep=$(basename "$d")
    case "$ep" in *"lv_${SHARD_ID}"*) ;; *) continue;; esac
    grep -qxF "$ep" "$LEDGER" && continue
    acc="$d/acceptance.json"
    [ -f "$acc" ] || continue
    age=$(( $(date +%s) - $(stat -c %Y "$acc") ))
    # Too fresh to trust: package.py may still be writing beside it. Mark it pending so the stop
    # marker cannot end the loop while an episode is sitting here waiting to be old enough.
    [ "$age" -lt 120 ] && { PENDING=1; continue; }
    # Three outcomes, not two. `accepted: true` and `accepted: false` are verdicts; `null` means
    # the gates did not run, which is UNJUDGED - and an unjudged episode must not be filed under
    # _rejected/, because that would assert a failure nobody measured. It goes to the normal
    # prefix with its acceptance.json saying so, and longvideo/readjudicate.py can compute the
    # verdict later from the upload.
    #
    # A rejected episode is still uploaded, under _rejected/, rather than deleting hours of
    # recording over one gate. What must never happen is a rejected episode landing where an
    # accepted one would - the acceptance.json travels with it either way.
    verdict=$(python3 -c "
import json
a = json.load(open('$acc')).get('accepted')
print('unjudged' if a is None else ('accepted' if a else 'rejected'))" 2>/dev/null)
    case "$verdict" in
      rejected)
        DST="$DSTROOT/_rejected/$ep/"
        echo "$(date '+%H:%M') rejected (uploading under _rejected/) $ep" >> "$LOG" ;;
      unjudged)
        DST="$DSTROOT/$ep/"
        echo "$(date '+%H:%M') unjudged - gates not run (uploading to the normal prefix) $ep" \
          >> "$LOG" ;;
      *)
        DST="$DSTROOT/$ep/" ;;
    esac
    if aws s3 sync "$d" "$DST" --exclude "rgb.mp4" --exclude "rgb_proxy.mp4" \
         --exclude "rgb_keyframes/*" --exclude "preview.mp4" --only-show-errors; then
      echo "$ep" >> "$LEDGER"
      echo "$(date '+%H:%M') uploaded $ep -> $DST" >> "$LOG"
    else
      echo "$(date '+%H:%M') UPLOAD-FAILED $ep (will retry)" >> "$LOG"
      PENDING=1
    fi
  done
  python3 - <<PY 2>/dev/null
import json, subprocess, time
from pathlib import Path
p = Path("$L/state_${SHARD_ID}.json")
st = json.loads(p.read_text()) if p.exists() else {}
up = [l for l in open("$LEDGER").read().splitlines() if l.strip()]
body = json.dumps({"shard": "$SHARD_ID", "slug": "$SLUG", "done": st.get("done"),
                   "attempts": st.get("attempts", []), "uploaded": up,
                   "ts": time.strftime("%F %T")}, indent=1)
subprocess.run(["aws", "s3", "cp", "-", "$STATUS/${SHARD_ID}.json"],
               input=body.encode(), check=False)
PY
  # The stop marker means "no more episodes are coming", NOT "stop mid-job". Breaking on it
  # unconditionally threw away the retry: a sync can fail once and succeed on the next pass (one
  # did, on shard s03), but boot.sh sets the marker on a timer, so if it was already set when a
  # sync failed the loop exited and left 200 GB on a disk about to be powered off. Keep going
  # while anything is still unuploaded, and say so.
  if [ -f "$L/stop_uploads_${SHARD_ID}" ]; then
    if [ "${PENDING:-0}" = "0" ]; then
      echo "$(date '+%H:%M') stop marker, nothing pending" >> "$LOG"; break
    fi
    echo "$(date '+%H:%M') stop marker set, but an upload is still pending - retrying" >> "$LOG"
  fi
  PENDING=0
  sleep 300
done
