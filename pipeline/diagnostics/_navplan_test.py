import sys, json
P = "/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline"
sys.path.insert(0, P)
import engine, plan_navmesh
ucv = engine.connect(1280, 720, timeout=600); req = ucv.client.request
SP = (-18076.2, -3605.6, 270.0)
r = engine.nav_ensure(req, SP[:2], SP[2])
print("ensure:", {k: r.get(k) for k in ("ok","synthesised","bounds_volumes",
                                        "volumes_covering_region","has_navmesh","build_wait_s")})
if r.get("note"): print("  note:", r["note"][:150])
e = engine.nav_export(req, "/home/ue4/nb2.bin", "/home/ue4/nb2.json")
print("export:", {k: e.get(k) for k in ("ok","vertex_count","triangle_count","bounds_min","bounds_max")})
if e.get("ok"):
    nav, meta = plan_navmesh.load("/home/ue4/nb2.bin", "/home/ue4/nb2.json")
    res = plan_navmesh.plan(nav, meta, 1000.0, 2400.0, min_clear_cm=150.0, anchor_hint=SP)
    print("\nplan ok:", res["ok"])
    if not res["ok"]: print(" ", res["error"])
