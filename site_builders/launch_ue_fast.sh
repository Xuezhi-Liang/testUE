#!/usr/bin/env bash
# Launch UE for data collection only - no Pixel Streaming.
#
# tools/launch_game_unreal.sh also starts the Pixel Streaming encoder, connects to the
# signalling server and accepts external viewers on 8408. None of that is used by the
# UnrealCV capture path, it costs GPU/CPU per frame, and that port is unauthenticated
# and world-open. Stripping it leaves UnrealCV on -cvport as the only interface.
set -uo pipefail

UE_ROOT=${UE_ROOT:-/home/ue4/UnrealEngine}
UPROJ=${UPROJ:-/home/ue4/simworld/gym_citynav.uproject}
GAME_MAP=${GAME_MAP:-/Game/TokyoStylizedEnvironment/Maps/Tokyo}
UNREALCV_PORT=${UNREALCV_PORT:-9208}
HOOK=${HOOK:-/home/ue4/tools/start_hook.py}

echo "[launch_fast] map=$GAME_MAP  cvport=$UNREALCV_PORT  (no pixel streaming)"

exec "$UE_ROOT/Engine/Binaries/Linux/UnrealEditor" "$UPROJ" "$GAME_MAP" \
  -RenderOffscreen -unattended -nosplash -nop4 -NoSound -nopause \
  -NoVerifyGC -noailogging \
  --ExecutePythonScript="$HOOK" \
  -cvport "$UNREALCV_PORT" \
  -log
