// Minimal C++ surface for the two things this dataset needs and Python/UnrealCV cannot do.
//
// Borrowed from UE5-Agent-Data (WorldModelCollect). That project already paid for the
// debugging behind both recipes; the comments carrying a "PITFALL" tag are the ones where the
// obvious implementation produces plausible-looking wrong data rather than an error, and they
// must not be simplified away.
//
// Why C++ at all, when everything else here is driven from Python over UnrealCV:
//
//   1. DEPTH. `vget /camera/N/depth npy` returns a constant 65504 (fp16 max) on this build,
//      and every attempt to read a render target from editor Python asserts at
//      VulkanRenderTarget.cpp:171 and takes the editor down. The cause is not our request:
//      FRenderTarget::ReadLinearColorPixels on Vulkan is not a float path at all, and for
//      RTF_R32f the format is absent from the RHI's convert-to-FColor switch so it fires a
//      checkf. Depth has to come back through FRHIGPUTextureReadback, which is
//      format-agnostic and is not reachable from Python.
//
//   2. NAVMESH. Downtown_West ships no ANavMeshBoundsVolume, so every NavigationSystemV1
//      query returns None and spec section 11's path-legality check cannot run. Python can
//      spawn the volume and can call OnNavigationBoundsUpdated, but UCubeBuilder, UModel and
//      UPolys are not exposed - and a volume spawned from code has no brush, so without them
//      its bounds stay degenerate and the build covers nothing.
//
// Everything else - route planning, pose recording, packaging, QA - stays in Python.

#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "SimWorldCapture.generated.h"

UCLASS()
class USimWorldCapture : public UBlueprintFunctionLibrary
{
	GENERATED_BODY()

public:
	/**
	 * Render scene depth at a pose and write it as EXR, linear METRES in the R channel.
	 *
	 * Conventions, chosen to match UE5-Agent-Data so a reader written for one works on both:
	 *   - planar depth (distance along the camera's view axis), not radial;
	 *   - -1 marks sky, unwritten pixels and anything past MaxRangeM. Not a measurement.
	 *     -1 rather than 0 because 0 is also what an uninitialised buffer holds, and "sky"
	 *     must not look identical to "the capture failed"; not NaN because NaN poisons
	 *     aggregates; unprojecting a -1 puts the point behind the camera, which is a loud
	 *     failure rather than an invented wall.
	 *   - float16 is enough: its precision is relative (~5e-4), so in metres it reaches 65 km
	 *     with 1 mm at 1 m and 6 cm at 100 m.
	 *
	 * Writes nothing on a failed readback and says so in the returned JSON. A missing file is
	 * noticed; a file full of zeros is not.
	 *
	 * The JSON carries the measured statistics (valid fraction, min, max, centre pixel) so a
	 * caller can verify the depth without depending on its own EXR reader - ours turned out to
	 * be an opencv build with `OpenEXR: NO`, which made a perfectly good file look corrupt.
	 */
	UFUNCTION(BlueprintCallable, Category = "SimWorld|Capture",
		meta = (WorldContext = "WorldContextObject"))
	static FString CaptureDepthEXR(UObject* WorldContextObject, FVector Location,
		FRotator Rotation, float FOVDegrees, int32 Width, int32 Height, const FString& OutPath,
		float MaxRangeM = 1000.f, bool bFloat16 = true);

	/**
	 * Make sure the level has navigation data covering a region, building it if needed.
	 *
	 * The synthesised volume is NOT saved back into the map. `bWasSynthesised` tells the
	 * caller which it got, because "authored navmesh" and "one we invented for this run" are
	 * different provenance and the dataset has to state which.
	 */
	UFUNCTION(BlueprintCallable, Category = "SimWorld|Nav",
		meta = (WorldContext = "WorldContextObject"))
	static FString EnsureNavMesh(UObject* WorldContextObject, FVector RegionCenter,
		FVector RegionExtent, float PaddingCm, float TimeoutSeconds);

	/**
	 * Poll the navigation build. Ready means `in_progress` false AND `has_navmesh` true, held for
	 * several consecutive polls.
	 *
	 * Separate from EnsureNavMesh deliberately. The build only progresses when the engine ticks, so
	 * a call that waits for it by ticking the navigation system itself re-enters that system: it
	 * hung one map for 42 minutes at 584% CPU with a silent log and never reached its own timeout.
	 * "Not in progress" alone is also not "finished" - a build that has not started yet reports
	 * idle too.
	 */
	UFUNCTION(BlueprintCallable, Category = "SimWorld|Nav",
		meta = (WorldContext = "WorldContextObject"))
	static FString NavBuildStatus(UObject* WorldContextObject);

	/** Project a point onto the navmesh. Returns false when there is no navmesh or no hit. */
	UFUNCTION(BlueprintCallable, Category = "SimWorld|Nav",
		meta = (WorldContext = "WorldContextObject"))
	static FString ProjectToNav(UObject* WorldContextObject, FVector Point, FVector QueryExtent);

	/**
	 * A path on the navmesh, as points. Empty when unreachable.
	 *
	 * This is what makes a route walkable by construction instead of by capsule-sweep
	 * guesswork: our sweeps trace TRACE_TYPE_QUERY1, which is the Visibility channel and not
	 * what blocks a Pawn, so a route can sweep perfectly clean and still contain a point the
	 * character cannot stand on. That cost several full capture cycles to discover.
	 */
	UFUNCTION(BlueprintCallable, Category = "SimWorld|Nav",
		meta = (WorldContext = "WorldContextObject"))
	static FString FindNavPath(UObject* WorldContextObject, FVector Start, FVector Goal);

	/**
	 * Run a whole frozen trajectory inside the engine tick and return immediately.
	 *
	 * This replaces the external per-frame loop. That loop cost 531 ms per frame at 1280x720, of
	 * which roughly 250-280 ms was UnrealCV round trips - every request costs ~18 ms whatever it
	 * does, and `place_camera` alone was 128 ms of pure protocol. Here the capture components go
	 * straight to the frozen pose, so there is no pawn, no teleport and no residual correction,
	 * and desired == actual by construction.
	 *
	 * Poll GetCaptureStatus() until `finished` is true.
	 */
	UFUNCTION(BlueprintCallable, Category = "SimWorld|Capture",
		meta = (WorldContext = "WorldContextObject"))
	static FString StartCapture(UObject* WorldContextObject, const FString& FrozenJsonPath,
		const FString& OutDir, int32 Width, int32 Height, float FOVDegrees,
		float DepthMaxRangeM = 1000.f, bool bDepthFloat16 = true, int32 JpegQuality = 92,
		bool bWriteRgb = true, float ExposureBiasEV = 0.f, bool bManualExposure = false,
		float AutoExposureSpeed = 0.f, float AutoExposureMinBrightness = 0.f,
		float AutoExposureMaxBrightness = 0.f, float LocalExposureShadow = 0.f,
		float LocalExposureHighlight = 0.f, float LocalExposureDetail = 0.f, int32 HistoryMode = 0,
		int32 SuperSample = 1);

	/**
	 * Spawn the fixed lighting rig (lighting_rig.py explains why and where the numbers are from):
	 * a Movable directional light, a sky atmosphere, a Movable real-time-capture sky light and a
	 * height fog, all tagged "SimWorldRig". Spawned from C++ because in a PIE session
	 * EditorLevelLibrary.spawn_actor_from_class returns None - it targets the editor world, not
	 * the one being captured. Hiding the level's own lighting stays in Python; that works on
	 * existing actors of either world.
	 */
	UFUNCTION(BlueprintCallable, Category = "SimWorld|Lighting",
		meta = (WorldContext = "WorldContextObject"))
	static FString SpawnLightingRig(UObject* WorldContextObject, float SunIntensity, float SunPitch,
		float SunYaw, float SunSourceAngle, float SkyIntensity, float FogDensity);

	/**
	 * A Movable, real-time-capture sky light at a stated intensity, tagged "SimWorldFillSky".
	 * The fill scheme's only spawned actor: the level's own sky light is often Static with no
	 * built data and does not come back to life from Python property writes in PIE - x1/x2/x3
	 * recorded identical frames - while a sky light this module spawns lights the shadows.
	 */
	UFUNCTION(BlueprintCallable, Category = "SimWorld|Lighting",
		meta = (WorldContext = "WorldContextObject"))
	static FString SpawnSkyLight(UObject* WorldContextObject, float Intensity, bool bRealTimeCapture = false);

	/** SetIntensity + MarkRenderStateDirty on every light component of the actor with this name. */
	UFUNCTION(BlueprintCallable, Category = "SimWorld|Lighting",
		meta = (WorldContext = "WorldContextObject"))
	static FString SetLightIntensity(UObject* WorldContextObject, const FString& ActorName, float Intensity);

	/**
	 * One tonemapped RGB frame from a pose, as PNG, at a stated exposure. For calibrating a
	 * map's exposure bias before recording it: render the same poses at several biases and pick
	 * the one whose histogram is right. With bManualExposure false the frame is what the level's
	 * own auto-exposure produces from a cold view state - the "current behaviour" reference.
	 * Returns mean 8-bit luminance and the blown / near-black fractions so the caller does not
	 * need to decode the PNG to rank biases.
	 */
	UFUNCTION(BlueprintCallable, Category = "SimWorld|Capture",
		meta = (WorldContext = "WorldContextObject"))
	static FString CaptureRgbPng(UObject* WorldContextObject, FVector Location,
		FRotator Rotation, float FOVDegrees, int32 Width, int32 Height, const FString& OutPath,
		float ExposureBiasEV = 0.f, bool bManualExposure = false, float LocalExposureShadow = 0.f,
		float LocalExposureHighlight = 0.f, float LocalExposureDetail = 0.f);

	UFUNCTION(BlueprintCallable, Category = "SimWorld|Capture",
		meta = (WorldContext = "WorldContextObject"))
	static FString GetCaptureStatus(UObject* WorldContextObject);

	/**
	 * Same capture, but with a person walking the trajectory and the camera chasing them.
	 *
	 * The frozen route is reused unchanged - navmesh planned, depth probed, corridor swept - and
	 * reinterpreted: it describes where the walker's eyes are, so the body goes one eye height
	 * below it and the camera is placed behind and above.
	 *
	 * MeshPathsCsv is ordered: the first mesh leads the pose, the rest follow it as garments. A
	 * CitySampleCrowd person is four separate skeletal meshes on one shared SK_Base skeleton.
	 *
	 * AnimForwardSpeedCmS is the walk animation's own forward speed, which the caller measures
	 * from the root bone track. The animation is then positioned by distance travelled rather
	 * than by time, so the stride matches the ground and the feet do not slide.
	 */
	UFUNCTION(BlueprintCallable, Category = "SimWorld|Capture",
		meta = (WorldContext = "WorldContextObject"))
	static FString StartWalkerCapture(UObject* WorldContextObject, const FString& FrozenJsonPath,
		const FString& OutDir, int32 Width, int32 Height, float FOVDegrees,
		const FString& MeshPathsCsv, const FString& WalkAnimPath, const FString& FacePath,
		const FString& WalkerBlueprintPath, float EyeHeightCm, float AnimForwardSpeedCmS, float CamBehindCm = 260.f,
		float CamAboveCm = 40.f, float CamLookAtZCm = 110.f, float CamSideCm = 70.f,
		float CamPitchDeg = -10.f, bool bCameraFollowsView = true, float DepthMaxRangeM = 1000.f,
		bool bDepthFloat16 = true, int32 JpegQuality = 92);

	/** Export the navmesh triangles so routes can be planned offline, before any GPU time. */
	UFUNCTION(BlueprintCallable, Category = "SimWorld|Nav",
		meta = (WorldContext = "WorldContextObject"))
	static FString ExportNavMesh(UObject* WorldContextObject, const FString& OutBinPath,
		const FString& OutJsonPath);
};
