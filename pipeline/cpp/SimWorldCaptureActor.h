// The per-frame capture loop, moved inside the engine tick.
//
// Why this exists. The Python loop it replaces talked to the editor a dozen times per frame over
// UnrealCV, and every request costs ~18 ms regardless of what it does (measured: a bare `vrun py`
// round trip with no work is 17.9 ms). That put a 1280x720 frame at 531 ms, of which roughly
// 250-280 ms was waiting on a socket - `place_camera` alone was 128 ms of pure protocol with no
// rendering in it at all. UE5-Agent-Data does the whole loop inside one actor's Tick and measures
// 38.7 ms per frame end to end: less than our single place_camera call. The difference is
// architecture, not hardware.
//
// Three properties this shape buys that the external loop could not have:
//
//   1. One frame clock. `FrameIndex` advances in exactly one place and every modality for that
//      frame is captured and read inside the same tick, so alignment is structural rather than
//      something to verify afterwards.
//   2. A fixed timestep that means something. `-usefixedtimestep -fps=24` only aligns state to
//      pixels if the capture happens inside the tick; an external loop cannot guarantee one tick
//      per frame, so the flag on its own was decoration.
//   3. No pawn. A frozen trajectory names the camera pose directly, so the capture components go
//      there and desired == actual by construction. That removes the teleport, the two-pass
//      residual correction, and the constraint that the eye height is whatever the character
//      capsule happens to give.
//
// TG_PostUpdateWork: after movement, physics and camera updates have settled for the step, so the
// state written and the pixels rendered describe the same instant.

#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "HAL/ThreadSafeCounter.h"
#include "SimWorldCaptureActor.generated.h"

class USceneCaptureComponent2D;
class UTextureRenderTarget2D;

USTRUCT()
struct FSimWorldPose
{
	GENERATED_BODY()

	FVector Location = FVector::ZeroVector;
	FRotator Rotation = FRotator::ZeroRotator;
	FString Phase;
};

UCLASS()
class ASimWorldCaptureActor : public AActor
{
	GENERATED_BODY()

public:
	ASimWorldCaptureActor();

	/** Parse a frozen trajectory and arm the actor. Returns an error string, empty on success. */
	FString Arm(const FString& FrozenJsonPath, const FString& OutDir, int32 InWidth,
		int32 InHeight, float InFov, float InDepthMaxRangeM, bool bInDepthFloat16,
		int32 InJpegQuality, bool bInWriteRgb, float InExposureBiasEV = 0.f,
		bool bInManualExposure = false, float InAutoExposureSpeed = 0.f,
		float InAutoExposureMinBrightness = 0.f, float InAutoExposureMaxBrightness = 0.f,
		float InLocalExposureShadow = 0.f, float InLocalExposureHighlight = 0.f,
		float InLocalExposureDetail = 0.f, int32 InHistoryMode = 0, int32 InSuperSample = 1);

	/**
	 * Optional: put a walking person on the trajectory and film them from behind instead of
	 * looking out through their eyes.
	 *
	 * The frozen trajectory is reused unchanged - the same navmesh-planned route, depth probe and
	 * corridor sweep - but it now describes where the PERSON is rather than where the camera is.
	 * The camera is derived from it: behind and above, aimed at the walker's chest.
	 *
	 * `MeshPathsCsv` is comma separated and ORDER MATTERS. The first mesh is the leader whose
	 * animation drives the pose; the rest are garments set to follow it via
	 * SetLeaderPoseComponent. CitySampleCrowd people are modular - base body, top, trousers,
	 * shoes are four separate skeletal meshes on one shared SK_Base skeleton - so this is the
	 * only way to dress one.
	 *
	 * `AnimForwardSpeedCmS` is the animation's OWN forward speed, measured by the caller from the
	 * root bone track. It is a parameter rather than something extracted here because it is what
	 * prevents foot sliding: the animation is positioned by distance travelled, not by time, so
	 * the feet advance exactly as far as the body does. Passing 0 disables that and plays the
	 * animation at wall-clock rate, which slides.
	 */
	FString AttachWalker(const FString& MeshPathsCsv, const FString& WalkAnimPath,
		const FString& FacePath, const FString& BlueprintPath, float InEyeHeightCm,
		float AnimForwardSpeedCmS, float BehindCm, float AboveCm, float LookAtZCm, float SideCm,
		float PitchDeg, bool bFollowView);

	/** Progress as JSON, for a caller polling from outside. */
	FString StatusJson() const;

	bool IsFinished() const { return bFinished; }

	virtual void Tick(float DeltaSeconds) override;

	/**
	 * Take the walker with us.
	 *
	 * The walker has to be its own actor - this one IS the camera, so a parented mesh would ride
	 * the camera instead of walking - but that also means destroying the capture actor leaves the
	 * walker standing in the level. Two runs in one editor session then render both, and the
	 * previous take's body was visible behind the current one in the footage.
	 */
	virtual void EndPlay(const EEndPlayReason::Type Reason) override;

private:
	void CaptureOneFrame();
	void Finish(const FString& Why);

	/**
	 * Place the walker for this frame and return where the chase camera goes.
	 *
	 * The animation is positioned from DISTANCE, not from time: AnimTime =
	 * fmod(travelled / anim's own speed, anim length). At the trajectory's speed that makes the
	 * stride length match the ground exactly, and it stays correct through the slow ramp in and
	 * out of every leg, which a fixed play rate would not.
	 */
	void PoseWalker(int32 Frame, FVector& OutCamLoc, FRotator& OutCamRot);

	/**
	 * Hand encoding and file writing to the thread pool.
	 *
	 * Measured before this existed: 131 ms per frame, of which render was 1 ms, readback 45 ms and
	 * write 72 ms - so more than half the frame was spent compressing and saving, on the game
	 * thread, while the GPU sat idle. The readback has to stay blocking (it is what guarantees the
	 * pixels belong to this step) but nothing about encoding does.
	 *
	 * Bounded on purpose. Each pending frame holds a full RGBA frame plus a float depth frame -
	 * about 7 MB at 1280x720 - so an unbounded queue would trade a throughput problem for a
	 * memory one and fall over on a long episode instead of a short one.
	 */
	void EnqueueWrite(int32 Frame, TArray<uint8>&& RgbBytes, TArray<FLinearColor>&& Depth);
	void WaitForSlot();
	void DrainAll();

	TArray<FSimWorldPose> Poses;
	FString OutputDir;
	FString StatesPath;
	FString FinishReason;

	int32 Width = 1280;
	int32 Height = 720;
	float Fov = 90.f;
	float DepthMaxRangeM = 1000.f;
	bool bDepthFloat16 = true;
	int32 JpegQuality = 92;
	bool bWriteRgb = true;
	// Exposure. Manual pins the RGB capture at one exposure for the whole episode: auto-exposure
	// adapts to what the camera has been looking at, so the same corner renders differently
	// depending on where the agent came from and the observation stops being a function of the
	// state. The task has declared `render.exposure: "fixed"` since the first episode; until
	// this field existed nothing applied it, and a same-pose brightness test on Tokyo measured
	// 14.6x the frame-to-frame noise floor. The bias is per map, chosen from the histogram the
	// level actually produces, not a taste.
	float ExposureBiasEV = 0.f;
	bool bManualExposure = false;
	// "Instant" auto-exposure: keep the level's metering but remove its lag. Path dependence
	// comes from the adaptation TIME, not from adapting: at a speed of ~100 EV/s the exposure is
	// a function of the current frame alone, so the same pose gives the same brightness whatever
	// the camera looked at before, while a view into shade still gets lifted the way an
	// auto-exposure viewer expects. 0 = leave the level's speed. Min/Max clamp the range (units
	// as the project's ExtendDefaultLuminanceRange setting dictates); 0/0 = leave.
	float AutoExposureSpeed = 0.f;
	float AutoExposureMinBrightness = 0.f;
	float AutoExposureMaxBrightness = 0.f;
	// Local exposure (see ApplyLocalExposure in the .cpp); 0 = leave the level's.
	float LocalExposureShadow = 0.f;
	float LocalExposureHighlight = 0.f;
	float LocalExposureDetail = 0.f;
	// RGB history modes. Legacy modes 0..3 keep SceneCapture's default TemporalAA=false:
	// selecting TSR/TAA by cvar alone falls back to FXAA (UE 5.8 SceneView.cpp).
	// 0 = persistent state; 1 = camera cut each frame; 2 = no view state (loses Lumen GI);
	// 3 = fresh capture each frame: low static noise, but moving fine detail aliases.
	// 4 = persistent state + explicit TemporalAA show flag + 32 rendered warmup frames.
	// Mode 4 reduces motion shimmer; see RENDER_QUALITY.md for measured static-noise tradeoffs.
	int32 HistoryMode = 0;
	// Spatial anti-aliasing by supersampling: the RGB target is rendered at Width*SS x Height*SS
	// and box-filtered down to Width x Height before encoding. Deterministic, no temporal state,
	// so it composes with HistoryMode 3. It exists because with a fresh view state per frame there
	// is no temporal AA at all, and a moving camera crawls over high-frequency texture (brick,
	// tiles) exactly as the persistent-state recordings did - one frame at a time it is aliasing,
	// in a video it reads as flicker. Depth is not supersampled: it is a measurement, and a
	// box-filtered depth edge is a depth that exists nowhere.
	int32 SuperSample = 1;
	int32 RgbWidth = 1280;
	int32 RgbHeight = 720;

	int32 FrameIndex = 0;
	bool bArmed = false;
	bool bFinished = false;

	// Ticks skipped before recording starts. The first tick after arming still carries the
	// previous view's temporal history (TSR/TAA accumulate across frames), and a capture taken
	// then is a blend of two poses that reads as motion blur nobody asked for.
	int32 WarmupTicks = 3;
	int32 WarmupDone = 0;

	double StartedSeconds = 0.0;
	double RenderSeconds = 0.0;
	double ReadbackSeconds = 0.0;
	double WriteSeconds = 0.0;

	TArray<FString> StateLines;

	FThreadSafeCounter PendingWrites;
	FThreadSafeCounter WriteFailures;
	int32 MaxPendingWrites = 8;
	class IImageWrapperModule* WrapperModule = nullptr;

	// The walker is a separate actor on purpose: this actor IS the camera, so anything parented to
	// it would ride along with the camera instead of walking the route.
	UPROPERTY() AActor* Walker = nullptr;
	UPROPERTY() TArray<class USkeletalMeshComponent*> WalkerParts;
	UPROPERTY() class USkeletalMeshComponent* FaceComponent = nullptr;
	UPROPERTY() class UAnimSequence* WalkAnim = nullptr;
	bool bWalkerMode = false;
	float EyeHeightCm = 170.f;
	float AnimSpeedCmS = 0.f;
	float RootMotionCm = 0.f;
	float WalkAnimLengthS = 0.f;
	float CamBehindCm = 260.f;
	float CamAboveCm = 40.f;
	float CamLookAtZCm = 110.f;
	float CamSideCm = 70.f;
	float CamPitchDeg = -10.f;
	// A Character's origin is its capsule centre, so its actor sits this far above its feet.
	float WalkerZOffsetCm = 0.f;
	// A pivot is charged this much animation distance per radian turned, so the feet step through a
	// turn instead of freezing. 45 cm makes a right angle cost roughly one stride.
	float TurnFootRadiusCm = 45.f;
	// Time constant of the camera's yaw lag. 0 would snap the world round during every turn.
	float CamYawLagS = 0.32f;
	float SmoothedCamYaw = 0.f;
	bool bCamYawInitialised = false;
	bool bCameraFollowsView = true;
	double TravelledCm = 0.0;

	UPROPERTY() USceneCaptureComponent2D* RgbCapture = nullptr;
	UPROPERTY() USceneCaptureComponent2D* DepthCapture = nullptr;
	UPROPERTY() UTextureRenderTarget2D* RgbTarget = nullptr;
	UPROPERTY() UTextureRenderTarget2D* DepthTarget = nullptr;
};
