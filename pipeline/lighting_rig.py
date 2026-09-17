#!/usr/bin/env python3
"""Replace a purchased level's global lighting with one known rig, for the session only.

The Dubai project (`~/dubai_ue_detail`) never has a dark or blown frame, and the reason is not a
clever exposure: it turns auto-exposure off for the whole project and lights the scene ITSELF, with
one sun, one sky and one haze whose intensities were chosen against that fixed exposure. A
purchased level ships its author's sun, sky blueprint, fog, clouds and post-process volumes, all
tuned for an auto-exposure that then drifts with wherever the camera has been - and with the
skylight often Static and unbuilt, so shadow renders at zero.

This applies the Dubai recipe to any level:

  1. hide every GLOBAL lighting component in the level - directional lights, sky lights, sky
     atmosphere, height fog, volumetric clouds - and disable every post-process volume and
     component (that is where the author's exposure, grading and bloom live). Local lights
     (point / spot / rect: street lamps, shop windows) are content and stay.
  2. spawn the rig with the Dubai values as finalised there (finalize_render_quality.py):
     Movable DirectionalLight 7.0, pitch -38 / yaw -55, atmosphere sun, 1 deg source angle;
     SkyAtmosphere; Movable SkyLight 2.5 with real-time capture; ExponentialHeightFog 0.006.
  3. auto-exposure off for the session: r.DefaultFeature.AutoExposure 0 (Dubai's ini line) and
     r.EyeAdaptationQuality 0 (belt and braces for scene captures), motion blur off.

Nothing is saved to the map. Everything hidden and everything spawned is returned and goes into
capture_summary.json, because "the map as shipped" and "the map under the rig" are different data.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine  # noqa: E402

DUBAI_RIG = {
    "sun_intensity": 7.0, "sun_pitch": -38.0, "sun_yaw": -55.0, "sun_source_angle": 1.0,
    "sky_intensity": 2.5, "fog_density": 0.006,
    "cvars": ["r.DefaultFeature.AutoExposure 0", "r.EyeAdaptationQuality 0",
              "r.DefaultFeature.MotionBlur 0", "r.MotionBlurQuality 0"],
    "source": "dubai_ue_detail: build_realism.py (rig) + finalize_render_quality.py (intensities) + Config/DefaultEngine.ini (AutoExposure=False)",
}

RIG_SCRIPT = """
import unreal as U
P = json.loads('''%(params)s''')
hidden, disabled, kept_local = [], [], 0
GLOBAL = (U.DirectionalLightComponent, U.SkyLightComponent, U.SkyAtmosphereComponent,
          U.ExponentialHeightFogComponent, U.VolumetricCloudComponent)
LOCAL = (U.PointLightComponent, U.SpotLightComponent, U.RectLightComponent)
for a in U.GameplayStatics.get_all_actors_of_class(w, U.Actor):
    name = str(a.get_name())
    if a.actor_has_tag("SimWorldRig"):
        continue
    for c in a.get_components_by_class(U.SceneComponent):
        if isinstance(c, GLOBAL):
            try:
                c.set_visibility(False, True)
                if isinstance(c, U.LightComponentBase):
                    c.set_editor_property("intensity", 0.0)
                hidden.append({"actor": name, "component": str(c.get_name()), "class": str(c.get_class().get_name())})
            except Exception as e:
                hidden.append({"actor": name, "component": str(c.get_name()), "error": str(e)[:120]})
        elif isinstance(c, LOCAL):
            kept_local += 1
    for c in a.get_components_by_class(U.PostProcessComponent):
        try:
            c.set_editor_property("enabled", False)
            disabled.append({"actor": name, "component": str(c.get_name()), "class": "PostProcessComponent"})
        except Exception as e:
            disabled.append({"actor": name, "component": str(c.get_name()), "error": str(e)[:120]})
    if isinstance(a, U.PostProcessVolume):
        try:
            a.set_editor_property("enabled", False)
            disabled.append({"actor": name, "class": "PostProcessVolume", "unbound": bool(a.get_editor_property("unbound"))})
        except Exception as e:
            disabled.append({"actor": name, "class": "PostProcessVolume", "error": str(e)[:120]})

spawned = json.loads(str(U.SimWorldCapture.spawn_lighting_rig(
    w, float(P["sun_intensity"]), float(P["sun_pitch"]), float(P["sun_yaw"]),
    float(P["sun_source_angle"]), float(P["sky_intensity"]), float(P["fog_density"]))))
if not spawned.get("ok"):
    RESULT.update({"ok": False, "error": "spawn_lighting_rig: " + str(spawned.get("error"))})
    raise SystemExit(0)
for cmd in P["cvars"]:
    U.SystemLibrary.execute_console_command(w, cmd)
RESULT.update({"ok": True, "hidden_global_lighting": hidden, "disabled_post_process": disabled,
               "kept_local_lights": kept_local, "rig": P,
               "spawned": spawned})
""".strip()


def apply_rig(req, params=None):
    p = dict(DUBAI_RIG, **(params or {}))
    r = engine.query(req, RIG_SCRIPT % {"params": json.dumps(p)}, timeout=600)
    if not r.get("ok"):
        return {"applied": False, "error": r.get("error"), "rig": p}
    print(f"[rig] hid {len(r['hidden_global_lighting'])} global lighting components, disabled "
          f"{len(r['disabled_post_process'])} post-process volumes/components, kept {r['kept_local_lights']} "
          f"local lights; spawned sun {p['sun_intensity']} / sky {p['sky_intensity']} / haze {p['fog_density']}; "
          f"auto-exposure off for the session")
    r["applied"] = True
    r["note"] = "session-only: the level's global lighting hidden and a fixed rig spawned; nothing saved to the map"
    return r


# ---------------------------------------------------------------------------------------------
# "fill": keep the author's sun, sky and grading; lift the shadows with THEIR sky light.
#
# The full rig above replaces the author's lighting and the colours go with it (a white noon sun
# under a painted sunset sky, the warm grade gone, blue-tinted shadows). The dark frames were never
# about the sun or the grade: they are shadow with too little ambient, on levels whose sky light
# was Static and unbuilt. So: make every sky light Movable + real-time capture (ensure_dynamic_sky
# already does) and multiply its intensity, leave everything else, and pin exposure on the capture
# (render.exposure "fixed" + the map's measured bias). Same picture as shipped, brighter shadows,
# one exposure.
FILL_SCRIPT = """
import unreal as U
F = float(%(factor)s)
level, ours = [], None
for a in U.GameplayStatics.get_all_actors_of_class(w, U.Actor):
    if a.actor_has_tag("SimWorldFillSky"):
        ours = a
        continue
    for c in a.get_components_by_class(U.SkyLightComponent):
        tags = list(a.get_editor_property("tags"))
        base = None
        for t in tags:
            if str(t).startswith("SimWorldFillBase:"):
                base = float(str(t).split(":", 1)[1])
        if base is None:
            base = float(c.get_editor_property("intensity"))
            tags.append(U.Name("SimWorldFillBase:%%.6f" %% base))
            a.set_editor_property("tags", tags)
        # the level's own sky light goes dark; ours replaces it at base x F
        c.set_visibility(False, True)
        level.append({"actor": str(a.get_name()), "base_intensity": base,
                      "mobility": str(c.get_editor_property("mobility"))})
if not level:
    RESULT.update({"ok": True, "no_skylight": True, "factor": F, "skylights": []})
    raise SystemExit(0)
base = level[0]["base_intensity"]
target = base * F
# Always a fresh sky light. Changing the intensity of the existing one and recapturing worked in
# a probe-only session and did NOT after a recording had run in between (x2 and x3 came back with
# the level's no-sky-light 13.5%% near-black while x1 had 5.1%%): the recapture never landed. A
# spawn captured the scene correctly every time it was tried, so destroy and spawn.
if ours is not None:
    ours.destroy_actor()
r = json.loads(str(U.SimWorldCapture.spawn_sky_light(w, target, False)))
if not r.get("ok"):
    RESULT.update({"ok": False, "error": str(r.get("error"))})
    raise SystemExit(0)
for cmd in ("r.DefaultFeature.AutoExposure 0", "r.EyeAdaptationQuality 0"):
    U.SystemLibrary.execute_console_command(w, cmd)
RESULT.update({"ok": True, "factor": F, "level_skylights_hidden": level, "base_intensity": base,
               "fill_skylight": r, "skylights": [{"actor": r.get("actor"), "base_intensity": base, "intensity": target}]})
""".strip()


def apply_fill(req, factor=2.0):
    r = engine.query(req, FILL_SCRIPT % {"factor": float(factor)}, timeout=300)
    if not r.get("ok"):
        return {"applied": False, "error": r.get("error"), "factor": factor}
    if r.get("no_skylight"):
        # A level with no sky light at all has nothing to multiply. Refuse rather than silently
        # record it at "fill x2" that did nothing; spawning one is a different provenance.
        return {"applied": False, "error": "level has no SkyLightComponent to boost", "factor": factor}
    print(f"[fill] sky light x{factor:g}: level sky light(s) {', '.join(l['actor'] for l in r['level_skylights_hidden'])} "
          f"hidden (base {r['base_intensity']:.2f}); ours {r['fill_skylight'].get('actor')} at {r['base_intensity']*factor:.2f}")
    r["applied"] = True
    r["note"] = ("session-only: the level's sky light hidden and a Movable captured-scene sky light spawned at "
                 "base x factor (captured-scene, not real-time: the painted sky sphere is what it must see); "
                 "sun, sky, fog and post-process untouched; auto-exposure cvars off")
    return r
