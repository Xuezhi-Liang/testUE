#include "SimWorldCaptureActor.h"

#include "Async/Async.h"
#include "Animation/AnimationAsset.h"
#include "Animation/AnimSequence.h"
#include "Components/SceneCaptureComponent2D.h"
#include "Components/CapsuleComponent.h"
#include "Components/SkeletalMeshComponent.h"
#include "GameFramework/Character.h"
#include "Engine/SkeletalMesh.h"
#include "Dom/JsonObject.h"
#include "Engine/TextureRenderTarget2D.h"
#include "Engine/World.h"
#include "IImageWrapper.h"
#include "IImageWrapperModule.h"
#include "ImageUtils.h"
#include "Math/Float16Color.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Modules/ModuleManager.h"
#include "RHIGPUReadback.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"

DEFINE_LOG_CATEGORY_STATIC(LogSimWorldCap, Log, All);

namespace
{
	/**
	 * Read a render target back as raw bytes through a GPU readback buffer.
	 *
	 * PITFALL - not FRenderTarget::ReadLinearColorPixels. On Vulkan that is not a float path: it
	 * calls the FColor overload and widens the result, so it quantises to 8 bits, and for R32F the
	 * format is absent from the RHI's convert-to-FColor switch so it fires a checkf that surfaces
	 * as SIGSEGV. FRHIGPUTextureReadback is format-agnostic and works for both targets here.
	 */
	struct FReadbackRequest
	{
		FTextureRenderTargetResource* Resource = nullptr;
		int32 BytesPerPixel = 4;
		TArray<uint8>* Out = nullptr;
		int32 Width = 0;      // 0 = the batch's Width/Height; set when a target has its own size
		int32 Height = 0;
	};

	/**
	 * Read several render targets back in ONE render command and ONE pipeline synchronisation.
	 *
	 * The previous version read each target with its own command, and each one carried a
	 * BlockUntilGPUIdle plus a FlushRenderingCommands. Two targets meant two full pipeline
	 * synchronisations per frame where one would do, and that showed up as 46 ms of the remaining
	 * 68 ms frame. UE5-Agent-Data's READBACK_PLAN.md makes the same point about its own code: a
	 * third-person frame synchronised three times, a pano frame eighteen.
	 *
	 * The readback stays BLOCKING. That is deliberate and is what guarantees these pixels belong
	 * to this simulation step rather than to whatever the GPU last finished; making it async is a
	 * separate change that has to argue about frame ownership.
	 *
	 * PITFALL - not FRenderTarget::ReadLinearColorPixels. On Vulkan that is not a float path: it
	 * calls the FColor overload and widens the result, so it quantises to 8 bits, and for R32F the
	 * format is absent from the RHI's convert-to-FColor switch so it fires a checkf that surfaces
	 * as SIGSEGV. FRHIGPUTextureReadback is format-agnostic and works for both targets here.
	 */
	bool ReadTargets(TArrayView<FReadbackRequest> Requests, int32 Width, int32 Height)
	{
		if (Requests.Num() == 0 || Width <= 0 || Height <= 0)
		{
			return false;
		}
		for (FReadbackRequest& R : Requests)
		{
			if (!R.Resource || !R.Out)
			{
				return false;
			}
			const int32 RW = R.Width > 0 ? R.Width : Width, RH = R.Height > 0 ? R.Height : Height;
			R.Out->SetNumUninitialized(RW * RH * R.BytesPerPixel);
		}

		bool bOk = false;
		bool* OkPtr = &bOk;
		FReadbackRequest* Reqs = Requests.GetData();
		const int32 Count = Requests.Num();

		ENQUEUE_RENDER_COMMAND(SimWorldBatchedReadback)(
			[Reqs, Count, Width, Height, OkPtr](FRHICommandListImmediate& RHICmdList)
			{
				TArray<TUniquePtr<FRHIGPUTextureReadback>> Readbacks;
				Readbacks.Reserve(Count);

				// Queue every copy first, then synchronise once for all of them.
				for (int32 i = 0; i < Count; ++i)
				{
					FRHITexture* Texture = Reqs[i].Resource->GetRenderTargetTexture();
					if (!Texture)
					{
						return;
					}
					Readbacks.Add(MakeUnique<FRHIGPUTextureReadback>(
						TEXT("SimWorldBatchedReadback")));
					// PITFALL - explicit origin and size. The defaulted FResolveRect is empty and
					// an empty rect is not reliably treated as "the whole texture": depth then
					// reads back as all zeros, files are written, frame counts match, nothing
					// errors.
					const int32 RW = Reqs[i].Width > 0 ? Reqs[i].Width : Width;
					const int32 RH = Reqs[i].Height > 0 ? Reqs[i].Height : Height;
					Readbacks[i]->EnqueueCopy(RHICmdList, Texture, FIntVector::ZeroValue, 0,
						FIntVector(RW, RH, 1));
				}
				RHICmdList.BlockUntilGPUIdle();

				for (int32 i = 0; i < Count; ++i)
				{
					int32 RowPitchInPixels = 0;
					const uint8* Src = static_cast<const uint8*>(
						Readbacks[i]->Lock(RowPitchInPixels, nullptr));
					if (!Src)
					{
						return;
					}
					// PITFALL - row pitch is in PIXELS and is usually padded past Width, and it is
					// per request. Indexing by Width shears the image progressively down the
					// frame.
					const int32 Bpp = Reqs[i].BytesPerPixel;
					// per-request size again: the first supersampled recording copied a 2560-wide
					// target with the 1280-pixel batch stride and delivered the top quarter, tiled
					const int32 CW = Reqs[i].Width > 0 ? Reqs[i].Width : Width;
					const int32 CH = Reqs[i].Height > 0 ? Reqs[i].Height : Height;
					const int64 SrcStride = static_cast<int64>(RowPitchInPixels) * Bpp;
					const int64 DstStride = static_cast<int64>(CW) * Bpp;
					uint8* Dst = Reqs[i].Out->GetData();
					for (int32 Y = 0; Y < CH; ++Y)
					{
						FMemory::Memcpy(Dst + Y * DstStride, Src + Y * SrcStride, DstStride);
					}
					Readbacks[i]->Unlock();
				}
				*OkPtr = true;
			});

		// One flush for the whole batch, not one per target.
		FlushRenderingCommands();
		return bOk;
	}

	FString Esc(const FString& In)
	{
		return In.Replace(TEXT("\\"), TEXT("\\\\")).Replace(TEXT("\""), TEXT("\\\""));
	}
}

ASimWorldCaptureActor::ASimWorldCaptureActor()
{
	PrimaryActorTick.bCanEverTick = true;
	// After movement, physics and camera updates have settled for this step, so the state logged
	// and the pixels rendered describe the same instant.
	PrimaryActorTick.TickGroup = TG_PostUpdateWork;
	RootComponent = CreateDefaultSubobject<USceneComponent>(TEXT("Root"));
}


namespace
{
	// Local exposure: per-region tone mapping. ShadowScale < 1 lifts shadows, HighlightScale < 1
	// holds highlights, independently; DetailStrength keeps local contrast so the result does not
	// go flat. Pure post-process on the capture - lighting, depth and the author's grade untouched.
	// 0 on a scale = leave that setting as the level has it.
	void ApplyLocalExposure(FPostProcessSettings& PP, float ShadowScale, float HighlightScale, float Detail)
	{
		if (ShadowScale <= 0.f && HighlightScale <= 0.f)
		{
			return;
		}
		PP.bOverride_LocalExposureMethod = true;
		PP.LocalExposureMethod = ELocalExposureMethod::Bilateral;
		if (ShadowScale > 0.f)
		{
			PP.bOverride_LocalExposureShadowContrastScale = true;
			PP.LocalExposureShadowContrastScale = ShadowScale;
		}
		if (HighlightScale > 0.f)
		{
			PP.bOverride_LocalExposureHighlightContrastScale = true;
			PP.LocalExposureHighlightContrastScale = HighlightScale;
		}
		if (Detail > 0.f)
		{
			PP.bOverride_LocalExposureDetailStrength = true;
			PP.LocalExposureDetailStrength = Detail;
		}
	}
}

FString ASimWorldCaptureActor::Arm(const FString& FrozenJsonPath, const FString& OutDir,
	int32 InWidth, int32 InHeight, float InFov, float InDepthMaxRangeM, bool bInDepthFloat16,
	int32 InJpegQuality, bool bInWriteRgb, float InExposureBiasEV, bool bInManualExposure,
	float InAutoExposureSpeed, float InAutoExposureMinBrightness, float InAutoExposureMaxBrightness,
	float InLocalExposureShadow, float InLocalExposureHighlight, float InLocalExposureDetail,
	int32 InHistoryMode, int32 InSuperSample)
{
	FString Raw;
	if (!FFileHelper::LoadFileToString(Raw, *FrozenJsonPath))
	{
		return FString::Printf(TEXT("could not read %s"), *FrozenJsonPath);
	}
	TSharedPtr<FJsonObject> Root;
	TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Raw);
	if (!FJsonSerializer::Deserialize(Reader, Root) || !Root.IsValid())
	{
		return TEXT("frozen trajectory is not valid JSON");
	}
	const TArray<TSharedPtr<FJsonValue>>* PoseArray = nullptr;
	if (!Root->TryGetArrayField(TEXT("poses"), PoseArray) || !PoseArray)
	{
		return TEXT("frozen trajectory has no 'poses' array");
	}

	Poses.Reset();
	for (const TSharedPtr<FJsonValue>& V : *PoseArray)
	{
		const TSharedPtr<FJsonObject>* O = nullptr;
		if (!V->TryGetObject(O) || !O)
		{
			continue;
		}
		FSimWorldPose P;
		P.Location = FVector(
			(*O)->GetNumberField(TEXT("x_cm")),
			(*O)->GetNumberField(TEXT("y_cm")),
			(*O)->GetNumberField(TEXT("z_cm")));
		// The frozen file stores pitch/yaw/roll by name, so there is no positional order to get
		// wrong. That matters: unreal.Rotator's positional order is (roll, pitch, yaw), and
		// passing a yaw in the second slot once aimed a depth capture straight at the floor and
		// produced a frame of entirely plausible readings that all equalled the eye height.
		P.Rotation = FRotator(
			(*O)->GetNumberField(TEXT("pitch_deg")),
			(*O)->GetNumberField(TEXT("yaw_deg")),
			(*O)->GetNumberField(TEXT("roll_deg")));
		(*O)->TryGetStringField(TEXT("phase"), P.Phase);
		Poses.Add(P);
	}
	if (Poses.Num() == 0)
	{
		return TEXT("frozen trajectory contains no usable poses");
	}

	OutputDir = OutDir;
	StatesPath = FPaths::Combine(OutDir, TEXT("engine_states.jsonl"));
	Width = InWidth;
	Height = InHeight;
	Fov = InFov;
	DepthMaxRangeM = InDepthMaxRangeM;
	bDepthFloat16 = bInDepthFloat16;
	JpegQuality = InJpegQuality;
	bWriteRgb = bInWriteRgb;
	ExposureBiasEV = InExposureBiasEV;
	bManualExposure = bInManualExposure;
	AutoExposureSpeed = InAutoExposureSpeed;
	AutoExposureMinBrightness = InAutoExposureMinBrightness;
	AutoExposureMaxBrightness = InAutoExposureMaxBrightness;
	LocalExposureShadow = InLocalExposureShadow;
	LocalExposureHighlight = InLocalExposureHighlight;
	LocalExposureDetail = InLocalExposureDetail;
	HistoryMode = InHistoryMode;
	if (HistoryMode < 0 || HistoryMode > 4)
	{
		return TEXT("history mode must be 0..4");
	}
	WarmupTicks = HistoryMode == 4 ? 32 : 3;
	SuperSample = FMath::Clamp(InSuperSample, 1, 4);
	RgbWidth = Width * SuperSample;
	RgbHeight = Height * SuperSample;

	IFileManager::Get().MakeDirectory(*FPaths::Combine(OutDir, TEXT("depth")), true);
	if (bWriteRgb)
	{
		IFileManager::Get().MakeDirectory(*FPaths::Combine(OutDir, TEXT("rgb")), true);
	}

	RgbTarget = NewObject<UTextureRenderTarget2D>(this);
	RgbTarget->RenderTargetFormat = RTF_RGBA8_SRGB;
	RgbTarget->ClearColor = FLinearColor::Black;
	RgbTarget->bAutoGenerateMips = false;
	RgbTarget->InitAutoFormat(RgbWidth, RgbHeight);
	RgbTarget->UpdateResourceImmediate(true);
	if (SuperSample > 1)
	{
		UE_LOG(LogSimWorldCap, Log, TEXT("rgb supersampled %dx: rendered %dx%d, delivered %dx%d"),
			SuperSample, RgbWidth, RgbHeight, Width, Height);
	}

	// R32f is required: SCS_SceneDepth writes linear centimetres, which an 8-bit target would
	// quantise into uselessness.
	DepthTarget = NewObject<UTextureRenderTarget2D>(this);
	DepthTarget->RenderTargetFormat = RTF_R32f;
	DepthTarget->ClearColor = FLinearColor::Black;
	DepthTarget->bAutoGenerateMips = false;
	DepthTarget->InitAutoFormat(Width, Height);
	DepthTarget->UpdateResourceImmediate(true);

	auto Make = [&](const TCHAR* Name, UTextureRenderTarget2D* Target, ESceneCaptureSource Src)
	{
		USceneCaptureComponent2D* C = NewObject<USceneCaptureComponent2D>(this, Name);
		C->SetupAttachment(RootComponent);
		C->RegisterComponent();
		C->TextureTarget = Target;
		C->CaptureSource = Src;
		// Manual capture only: an automatic per-frame capture fires at a point in the frame we do
		// not control, which is exactly how pixels and recorded pose drift apart.
		C->bCaptureEveryFrame = false;
		C->bCaptureOnMovement = false;
		// PITFALL - without a persistent view state the capture has no FSceneViewState, and
		// OverrideBlendableSettings returns immediately, so post-process materials are silently
		// dropped. It is also required for TSR/TAA history.
		C->bAlwaysPersistRenderingState = true;
		C->FOVAngle = Fov;
		C->ProjectionType = ECameraProjectionMode::Perspective;
		return C;
	};

	RgbCapture = Make(TEXT("SimWorldRgb"), RgbTarget, SCS_FinalColorLDR);
	DepthCapture = Make(TEXT("SimWorldDepth"), DepthTarget, SCS_SceneDepth);
	if (HistoryMode == 4)
	{
		// UE 5.8's SceneCaptureComponent2D constructor explicitly disables TemporalAA.
		// A project/cvar selecting TSR does NOT override this show flag: SceneView falls
		// back to FXAA. Keep the view state AND enable the capture's temporal path.
		RgbCapture->ShowFlags.SetAntiAliasing(true);
		RgbCapture->ShowFlags.SetTemporalAA(true);
		RgbCapture->ShowFlags.SetMotionBlur(false);
		RgbCapture->PostProcessSettings.bOverride_MotionBlurAmount = true;
		RgbCapture->PostProcessSettings.MotionBlurAmount = 0.f;
	}
	if (HistoryMode == 2)
	{
		RgbCapture->bAlwaysPersistRenderingState = false;
	}
	UE_LOG(LogSimWorldCap, Log, TEXT("rgb history mode %d, TemporalAA show flag %d, warmup %d"),
		HistoryMode, RgbCapture->ShowFlags.TemporalAA, WarmupTicks);
	if (bManualExposure)
	{
		// The component's own post-process settings sit above every PostProcessVolume in the
		// level, so this holds whatever exposure the purchased map ships with. Same override the
		// first pipeline's WMCCaptureActor makes, for the same reason. Manual mode meters from
		// the default camera (ISO 100, 1/60 s, f/4) plus this bias, so the bias IS the exposure.
		FPostProcessSettings& PP = RgbCapture->PostProcessSettings;
		PP.bOverride_AutoExposureMethod = true;
		PP.AutoExposureMethod = AEM_Manual;
		PP.bOverride_AutoExposureBias = true;
		PP.AutoExposureBias = ExposureBiasEV;
		RgbCapture->PostProcessBlendWeight = 1.f;
		UE_LOG(LogSimWorldCap, Log, TEXT("exposure pinned: manual, bias %.2f EV"), ExposureBiasEV);
	}
	else if (AutoExposureSpeed > 0.f)
	{
		FPostProcessSettings& PP = RgbCapture->PostProcessSettings;
		PP.bOverride_AutoExposureSpeedUp = true;
		PP.AutoExposureSpeedUp = AutoExposureSpeed;
		PP.bOverride_AutoExposureSpeedDown = true;
		PP.AutoExposureSpeedDown = AutoExposureSpeed;
		if (AutoExposureMaxBrightness > AutoExposureMinBrightness)
		{
			PP.bOverride_AutoExposureMinBrightness = true;
			PP.AutoExposureMinBrightness = AutoExposureMinBrightness;
			PP.bOverride_AutoExposureMaxBrightness = true;
			PP.AutoExposureMaxBrightness = AutoExposureMaxBrightness;
		}
		// A per-map offset can be combined - but only when one is asked for. Overriding the bias
		// with 0 REPLACES the author's own exposure bias (the level's post-process volume carries
		// one), and the first instant recordings came out 0.7 stops darker than the level as
		// shipped for exactly that reason: Downtown mean 84 against 140, ChemicalPlant 53 against 98.
		if (ExposureBiasEV != 0.f)
		{
			PP.bOverride_AutoExposureBias = true;
			PP.AutoExposureBias = ExposureBiasEV;
		}
		RgbCapture->PostProcessBlendWeight = 1.f;
		UE_LOG(LogSimWorldCap, Log, TEXT("exposure: level metering, instant (speed %.0f), range %.2f..%.2f, bias %.2f"),
			AutoExposureSpeed, AutoExposureMinBrightness, AutoExposureMaxBrightness, ExposureBiasEV);
	}
	else
	{
		UE_LOG(LogSimWorldCap, Warning, TEXT("exposure NOT pinned: the level's own auto-exposure "
			"decides brightness, so the same place will not render the same way twice"));
	}
	ApplyLocalExposure(RgbCapture->PostProcessSettings, LocalExposureShadow, LocalExposureHighlight, LocalExposureDetail);
	if (LocalExposureShadow > 0.f || LocalExposureHighlight > 0.f)
	{
		RgbCapture->PostProcessBlendWeight = 1.f;
		UE_LOG(LogSimWorldCap, Log, TEXT("local exposure: shadow %.2f highlight %.2f detail %.2f"),
			LocalExposureShadow, LocalExposureHighlight, LocalExposureDetail);
	}
	// Depth and instance ids are measurements, not photographs, and nothing in the tonemapping or
	// temporal path may touch them.
	DepthCapture->ShowFlags.SetAntiAliasing(false);
	DepthCapture->ShowFlags.SetMotionBlur(false);
	// PITFALL - bRenderInMainRenderer folds the depth capture into the main renderer instead of
	// running a second scene render, which looks like free performance. Headless with only scene
	// captures there is no main renderer view to attach to, and the target comes back all zeros:
	// valid-looking EXRs containing no depth.
	DepthCapture->bRenderInMainRenderer = false;

	WrapperModule = &FModuleManager::LoadModuleChecked<IImageWrapperModule>(TEXT("ImageWrapper"));
	PendingWrites.Reset();
	WriteFailures.Reset();

	StateLines.Reset();
	StateLines.Reserve(Poses.Num());
	FrameIndex = 0;
	WarmupDone = 0;
	bFinished = false;
	FinishReason.Reset();
	RenderSeconds = ReadbackSeconds = WriteSeconds = 0.0;
	StartedSeconds = FPlatformTime::Seconds();
	bArmed = true;

	UE_LOG(LogSimWorldCap, Display,
		TEXT("armed: %d poses, %dx%d, fov %.1f, depth %s, out %s"),
		Poses.Num(), Width, Height, Fov, bDepthFloat16 ? TEXT("f16") : TEXT("f32"), *OutputDir);
	return FString();
}

void ASimWorldCaptureActor::EndPlay(const EEndPlayReason::Type Reason)
{
	if (Walker)
	{
		Walker->Destroy();
		Walker = nullptr;
	}
	Super::EndPlay(Reason);
}

void ASimWorldCaptureActor::Tick(float DeltaSeconds)
{
	Super::Tick(DeltaSeconds);
	if (!bArmed || bFinished)
	{
		return;
	}
	if (WarmupDone < WarmupTicks)
	{
		// Park the captures on the first pose during warmup so the temporal history that TSR/TAA
		// accumulates belongs to the pose we are about to record, not to wherever the view was.
		SetActorLocationAndRotation(Poses[0].Location, Poses[0].Rotation);
		if (HistoryMode == 4 && bWriteRgb)
		{
			// Merely waiting ticks does not warm a manually triggered scene capture.
			// Render at the first pose, without advancing any dataset frame or logging it.
			RgbCapture->CaptureScene();
			FlushRenderingCommands();
		}
		++WarmupDone;
		return;
	}
	CaptureOneFrame();
	if (FrameIndex >= Poses.Num())
	{
		Finish(TEXT("reached the end of the frozen trajectory"));
	}
}

FString ASimWorldCaptureActor::AttachWalker(const FString& MeshPathsCsv,
	const FString& WalkAnimPath, const FString& FacePath, const FString& BlueprintPath,
	float InEyeHeightCm,
	float AnimForwardSpeedCmS, float BehindCm, float AboveCm, float LookAtZCm, float SideCm,
	float PitchDeg, bool bFollowView)
{
	UWorld* World = GetWorld();
	if (!World)
	{
		return TEXT("no world");
	}
	TArray<FString> MeshPaths;
	MeshPathsCsv.ParseIntoArray(MeshPaths, TEXT(","), true);
	if (MeshPaths.Num() == 0 && BlueprintPath.IsEmpty())
	{
		return TEXT("give either a mesh list or a walker blueprint");
	}

	WalkAnim = LoadObject<UAnimSequence>(nullptr, *WalkAnimPath);
	if (!WalkAnim)
	{
		return FString::Printf(TEXT("could not load walk animation %s"), *WalkAnimPath);
	}
	WalkAnimLengthS = WalkAnim->GetPlayLength();
	if (WalkAnimLengthS <= 0.f)
	{
		return FString::Printf(TEXT("walk animation %s has zero length"), *WalkAnimPath);
	}

	FActorSpawnParameters SpawnParams;
	SpawnParams.Name = MakeUniqueObjectName(World, AActor::StaticClass(),
		TEXT("SimWorldWalker"));
	FActorSpawnParameters SpawnParams2;
	SpawnParams2.Name = MakeUniqueObjectName(World, AActor::StaticClass(),
		TEXT("SimWorldWalkerBP"));
	Walker = World->SpawnActor<AActor>(AActor::StaticClass(), FVector::ZeroVector,
		FRotator::ZeroRotator, SpawnParams);
	if (!Walker)
	{
		return TEXT("could not spawn the walker actor");
	}
	// A bare AActor has no root component, and attaching to a null root leaves every mesh at the
	// world origin while the actor's own transform moves correctly - the same trap the depth
	// capture hit.
	USceneComponent* Root = NewObject<USceneComponent>(Walker, TEXT("WalkerRoot"));
	Walker->SetRootComponent(Root);
	Root->RegisterComponent();

	WalkerParts.Reset();

	// A blueprint, when one is given, instead of a mesh list.
	//
	// This is the difference between using the crowd and re-implementing it. A CitySampleCrowd
	// citizen is five layers across three skeletons, and hand-assembling one produced, in order: a
	// suit with an empty collar and sleeves (the `base` mesh carries material M_Hide and renders no
	// skin), then a stretched spike above the shoulder (a 232-bone SK_Base leader driving a
	// 167-bone metahuman_base_skel follower maps only the names that match), then an oversized head
	// at hip height (a MetaHuman face's origin is at the feet, so snapping it to a head bone puts
	// it nowhere near the head). BP_CrowdCharacter does all of it correctly on construction - the
	// editor log shows it building base, body, garments and shoes for one identity - so the only
	// thing left to do is take over the animation of whichever component drives the pose.
	if (!BlueprintPath.IsEmpty())
	{
		UClass* WalkerClass = LoadClass<AActor>(nullptr, *BlueprintPath);
		if (!WalkerClass)
		{
			Walker->Destroy();
			Walker = nullptr;
			return FString::Printf(TEXT("could not load walker blueprint class %s"),
				*BlueprintPath);
		}
		// At the first pose, not the origin, and forced.
		//
		// BP_CrowdCharacter is a Character: it carries a collision capsule, and the default spawn
		// collision handling refuses to place one that starts inside geometry. The world origin of a
		// city map is inside something, so SpawnActor returned null and the capture refused to
		// start. AlwaysSpawn plus bNoFail is correct here rather than a workaround: this walker is
		// driven kinematically frame by frame from a route that was already proved collision free,
		// so it must not be repositioned or rejected by physics at spawn.
		// The capsule offset is not known until after the spawn, so this lands one half height low
		// for a single frame and PoseWalker corrects it before anything is captured - the three
		// warmup ticks happen first.
		const FVector SpawnAt(Poses[0].Location.X, Poses[0].Location.Y,
			Poses[0].Location.Z - InEyeHeightCm);
		SpawnParams2.SpawnCollisionHandlingOverride =
			ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
		SpawnParams2.bNoFail = true;
		AActor* Spawned = World->SpawnActor<AActor>(WalkerClass, SpawnAt,
			FRotator(0.f, Poses[0].Rotation.Yaw, 0.f), SpawnParams2);
		if (!Spawned)
		{
			Walker->Destroy();
			Walker = nullptr;
			return FString::Printf(TEXT("could not spawn %s"), *BlueprintPath);
		}
		Walker->Destroy();          // the blueprint actor replaces the bare one
		Walker = Spawned;

		// Human size, because this blueprint does not spawn at one.
		//
		// BP_CrowdCharacter comes up with an actor scale of 2.0 in this project: its unscaled capsule
		// half height is the correct 88 cm and the scaled one is 176 cm, so the citizen stands about
		// 3.5 m tall. That is what put the head through the top of the frame at 4.8 m, and it also
		// breaks the gait: at twice size a walk cycle covers twice the ground, so positioning the
		// animation by the unscaled 140 cm/s advances the legs twice as far as the body moves and the
		// feet slide. Both come from the same number.
		Spawned->SetActorScale3D(FVector(1.f, 1.f, 1.f));

		// A Character's actor origin is the CENTRE of its collision capsule, not the soles of its
		// feet. Placing the actor at the foot position therefore buries it by one capsule half
		// height: the first take had the walker sunk to mid-thigh with road drawn across his legs,
		// which read as a framing problem and was not one. Measured from the capsule rather than
		// assumed, because the crowd's capsule is not the engine default.
		if (ACharacter* AsCharacter = Cast<ACharacter>(Walker))
		{
			if (UCapsuleComponent* Capsule = AsCharacter->GetCapsuleComponent())
			{
				WalkerZOffsetCm = Capsule->GetScaledCapsuleHalfHeight();
			}
		}

		TArray<USkeletalMeshComponent*> Comps;
		Walker->GetComponents<USkeletalMeshComponent>(Comps);
		USkeletalMeshComponent* Chosen = nullptr;
		for (USkeletalMeshComponent* C : Comps)
		{
			C->VisibilityBasedAnimTickOption =
				EVisibilityBasedAnimTickOption::AlwaysTickPoseAndRefreshBones;
			C->bEnableUpdateRateOptimizations = false;
			C->SetCollisionEnabled(ECollisionEnabled::NoCollision);
			// The pose is driven by whichever component shares the walk animation's skeleton and
			// is not already following another one. The blueprint's own leader/follower wiring is
			// left alone: the garments already follow, and re-pointing them would undo it.
			USkeletalMesh* M = C->GetSkeletalMeshAsset();
			if (!Chosen && M && M->GetSkeleton() == WalkAnim->GetSkeleton()
				&& C->LeaderPoseComponent == nullptr)
			{
				Chosen = C;
			}
		}
		if (!Chosen)
		{
			const FString Names = FString::JoinBy(Comps, TEXT(", "),
				[](USkeletalMeshComponent* C) { return C->GetName(); });
			Walker->Destroy();
			Walker = nullptr;
			return FString::Printf(
				TEXT("%s has no skeletal mesh component on the walk animation's skeleton (%s); "
					 "components: %s"), *BlueprintPath,
				WalkAnim->GetSkeleton() ? *WalkAnim->GetSkeleton()->GetName() : TEXT("none"),
				*Names);
		}
		WalkerParts.Add(Chosen);
		for (USkeletalMeshComponent* C : Comps)
		{
			if (C != Chosen)
			{
				WalkerParts.Add(C);
			}
		}
	}

	const int32 MeshCount = BlueprintPath.IsEmpty() ? MeshPaths.Num() : 0;
	for (int32 i = 0; i < MeshCount; ++i)
	{
		const FString Path = MeshPaths[i].TrimStartAndEnd();
		USkeletalMesh* Mesh = LoadObject<USkeletalMesh>(nullptr, *Path);
		if (!Mesh)
		{
			Walker->Destroy();
			Walker = nullptr;
			return FString::Printf(TEXT("could not load skeletal mesh %s"), *Path);
		}
		USkeletalMeshComponent* C = NewObject<USkeletalMeshComponent>(
			Walker, *FString::Printf(TEXT("WalkerPart%d"), i));
		C->SetupAttachment(Root);
		C->SetSkeletalMeshAsset(Mesh);
		// Without this the pose is only evaluated when something decides the mesh is visible, and
		// a scene capture is not the main view - the walker rendered in its bind pose, standing
		// rigid while sliding along the route.
		C->VisibilityBasedAnimTickOption =
			EVisibilityBasedAnimTickOption::AlwaysTickPoseAndRefreshBones;
		C->bEnableUpdateRateOptimizations = false;
		C->SetCollisionEnabled(ECollisionEnabled::NoCollision);
		C->RegisterComponent();
		WalkerParts.Add(C);
	}

	// The head, attached to a bone rather than leader-posed.
	//
	// A CitySampleCrowd person is five layers, and only two of them share a skeleton. `base`
	// carries material M_Hide - it is the body that hides UNDER clothes, so base plus garments
	// renders an empty collar and empty sleeves. The visible skin is `body` on
	// metahuman_base_skel (167 bones) and the head is a per-identity MetaHuman FaceMesh on
	// Face_Archetype_Skeleton; SK_Base has 232. Adding the skin as a leader-pose follower mapped
	// only the bones whose names matched and left the rest at their reference pose, which stretched
	// the mesh into a black spike above the shoulder.
	//
	// Attaching the face to the "head" bone instead sidesteps the mapping entirely. The head does
	// not then animate, which for a walk cycle filmed from behind is not visible - a walking head
	// barely moves relative to the neck - and it is stated in the episode metadata rather than
	// presented as a full crowd character.
	if (!FacePath.IsEmpty())
	{
		USkeletalMesh* Face = LoadObject<USkeletalMesh>(nullptr, *FacePath);
		if (!Face)
		{
			Walker->Destroy();
			Walker = nullptr;
			return FString::Printf(TEXT("could not load face mesh %s"), *FacePath);
		}
		FaceComponent = NewObject<USkeletalMeshComponent>(Walker, TEXT("WalkerFace"));
		FaceComponent->SetupAttachment(WalkerParts[0], TEXT("head"));
		FaceComponent->SetSkeletalMeshAsset(Face);
		FaceComponent->VisibilityBasedAnimTickOption =
			EVisibilityBasedAnimTickOption::AlwaysTickPoseAndRefreshBones;
		FaceComponent->SetCollisionEnabled(ECollisionEnabled::NoCollision);
		FaceComponent->RegisterComponent();
	}

	USkeletalMeshComponent* Leader = WalkerParts[0];
	Leader->SetAnimationMode(EAnimationMode::AnimationSingleNode);
	Leader->SetAnimation(WalkAnim);
	Leader->Stop();          // we position the animation ourselves, per frame
	if (BlueprintPath.IsEmpty())
	{
		for (int32 i = 1; i < WalkerParts.Num(); ++i)
		{
			// Garments carry no animation of their own; they are skinned to the leader's bones.
			WalkerParts[i]->SetLeaderPoseComponent(Leader);
		}
	}

	bWalkerMode = true;
	EyeHeightCm = InEyeHeightCm;

	// Measure the animation's own forward speed here rather than in Python.
	//
	// The Python route was UAnimationBlueprintLibrary::GetBonePoseForTime, which is deprecated
	// since 5.2 and, in this headless session, SPINS: three attempts left the editor at 600% CPU
	// with the game thread in R state, no log output, UnrealCV unresponsive, and - measured over
	// two minutes - the DDC completely untouched, 1904 MB and 907 files with zero writes. So it
	// was not compiling anything and waiting would not have helped; all three had to be killed.
	// ExtractRootMotionFromRange is the supported path and runs inside the module.
	AnimSpeedCmS = AnimForwardSpeedCmS;
	if (AnimSpeedCmS <= 0.f)
	{
		const FAnimExtractContext Ctx(0.0, true);
		const FTransform RootDelta = WalkAnim->ExtractRootMotionFromRange(
			0.0, static_cast<double>(WalkAnimLengthS), Ctx);
		RootMotionCm = RootDelta.GetTranslation().Size2D();
		AnimSpeedCmS = (WalkAnimLengthS > 0.f) ? RootMotionCm / WalkAnimLengthS : 0.f;
	}
	CamBehindCm = BehindCm;
	CamAboveCm = AboveCm;
	CamLookAtZCm = LookAtZCm;
	CamSideCm = SideCm;
	CamPitchDeg = PitchDeg;
	bCameraFollowsView = bFollowView;
	TravelledCm = 0.0;
	return FString();
}

void ASimWorldCaptureActor::PoseWalker(int32 Frame, FVector& OutCamLoc, FRotator& OutCamRot)
{
	const FSimWorldPose& P = Poses[Frame];

	// The frozen trajectory is an EYE path, so the person's feet are one eye height below it.
	const FVector Feet(P.Location.X, P.Location.Y, P.Location.Z - EyeHeightCm);
	// Where the ACTOR goes, which is not where the feet go when the walker is a Character.
	const FVector Placement(Feet.X, Feet.Y, Feet.Z + WalkerZOffsetCm);
	const FRotator Facing(0.f, P.Rotation.Yaw, 0.f);   // a walking body does not pitch or roll
	if (Walker)
	{
		Walker->SetActorLocationAndRotation(Placement, Facing);
	}

	if (Frame > 0)
	{
		TravelledCm += FVector::Dist2D(Poses[Frame].Location, Poses[Frame - 1].Location);

		// Turning costs the animation distance too, or the walker freezes mid-pivot.
		//
		// The animation is positioned by distance travelled, which is what stops the feet sliding.
		// But the trajectory's turn phases are pure rotation with no translation, so distance does
		// not advance and the character became a statue spinning on the spot - which is exactly what
		// "the camera is turning, not the person" looks like. Charging the turn an arc length puts
		// steps back into it: the radius is picked so a right-angle pivot costs about one stride
		// rather than measured off a real gait, and it is a parameter because that is a judgement.
		const float YawDelta = FMath::Abs(FMath::FindDeltaAngleDegrees(
			Poses[Frame - 1].Rotation.Yaw, Poses[Frame].Rotation.Yaw));
		TravelledCm += FMath::DegreesToRadians(YawDelta) * TurnFootRadiusCm;
	}
	if (WalkerParts.Num() > 0 && WalkAnim)
	{
		USkeletalMeshComponent* Leader = WalkerParts[0];
		const float T = (AnimSpeedCmS > 1.f)
			? FMath::Fmod(static_cast<float>(TravelledCm) / AnimSpeedCmS, WalkAnimLengthS)
			: FMath::Fmod(Frame / 24.f, WalkAnimLengthS);
		Leader->SetPosition(T, false);
		// Evaluate now. This runs in TG_PostUpdateWork, after the component's own tick group has
		// already passed for this step, so without forcing it the capture below would render the
		// pose from the PREVIOUS frame.
		Leader->TickAnimation(0.f, false);
		Leader->RefreshBoneTransforms();
	}

	// Chase camera, derived from the same pose rather than smoothed, so it is exactly as
	// reproducible as the trajectory.
	//
	// Two aims, and the difference is what the shot is about. Aimed at the chest, the camera looks
	// down at a back and the street ahead is squeezed out of frame. Following the walker's own view
	// rotation instead puts the camera over their shoulder looking where they look, so the person
	// and what they are walking towards are both in shot - which is what the trajectory is a record
	// of. The side offset keeps the body out of the centre of the frame.
	// The camera yaw LAGS the walker's heading rather than copying it.
	//
	// Copying it means the whole world snaps round in the six frames of a turn, and what reads on
	// screen is a camera being rotated, not a person turning. A first-order lag with a short time
	// constant makes the camera swing round behind them the way a following shot does. It stays
	// exactly reproducible: it is a causal filter over the frozen poses, so the same trajectory
	// always yields the same camera.
	if (!bCamYawInitialised)
	{
		SmoothedCamYaw = P.Rotation.Yaw;
		bCamYawInitialised = true;
	}
	else
	{
		const float Alpha = 1.f - FMath::Exp(-(1.f / 24.f) / FMath::Max(CamYawLagS, 1e-3f));
		SmoothedCamYaw = FRotator::NormalizeAxis(SmoothedCamYaw
			+ FMath::FindDeltaAngleDegrees(SmoothedCamYaw, P.Rotation.Yaw) * Alpha);
	}
	const FVector Fwd = FRotator(0.f, SmoothedCamYaw, 0.f).Vector();
	const FVector Side = FVector(-Fwd.Y, Fwd.X, 0.f);
	OutCamLoc = Feet - Fwd * CamBehindCm + Side * CamSideCm
		+ FVector(0.f, 0.f, EyeHeightCm + CamAboveCm);
	if (bCameraFollowsView)
	{
		// The walker's own heading, plus a small downward tilt.
		//
		// Copying the view rotation exactly points the camera dead level, and from 40 cm above the
		// eye line that put the walker's head and shoulders on the bottom edge of the frame with the
		// whole body out of shot - you could see where they were going but not that they were
		// walking. The tilt keeps the heading (which is the point of following the view) and brings
		// the body into the lower third.
		OutCamRot = FRotator(P.Rotation.Pitch + CamPitchDeg, SmoothedCamYaw, 0.f);
	}
	else
	{
		const FVector LookAt = Feet + FVector(0.f, 0.f, CamLookAtZCm);
		OutCamRot = (LookAt - OutCamLoc).Rotation();
	}
}

void ASimWorldCaptureActor::CaptureOneFrame()
{
	const int32 F = FrameIndex;
	const FSimWorldPose& P = Poses[F];

	// One place, one clock. Everything below describes frame F.
	FVector CamLoc = P.Location;
	FRotator CamRot = P.Rotation;
	if (bWalkerMode)
	{
		PoseWalker(F, CamLoc, CamRot);
	}
	SetActorLocationAndRotation(CamLoc, CamRot);

	double T0 = FPlatformTime::Seconds();
	// Each CaptureScene renders the whole scene again. With RGB and depth as separate captures
	// that is two full scene renders per frame, and the blocking readback below is mostly waiting
	// for them - which is why collapsing the two synchronisations into one saved 1 ms out of 46.
	// Gating RGB here makes that measurable: a depth-only frame isolates the cost of one render.
	if (bWriteRgb)
	{
		if (HistoryMode == 1)
		{
			RgbCapture->bCameraCutThisFrame = true;
		}
		else if (HistoryMode == 3)
		{
			// A fresh component - and so a fresh FSceneViewState with its frame counter at 0 -
			// for every frame, which is exactly what CaptureRgbPng does and what rendered the same
			// pose identically six times over (probe: 0.00) while the persistent component's
			// static frames differ by 1.8/255. The template copy carries every UPROPERTY across:
			// target, capture source, flags, FOV, and the post-process settings the exposure and
			// local-exposure code set on the original.
			USceneCaptureComponent2D* Fresh = NewObject<USceneCaptureComponent2D>(this, NAME_None, RF_NoFlags, RgbCapture);
			Fresh->SetupAttachment(RootComponent);
			Fresh->RegisterComponent();
			Fresh->SetWorldLocationAndRotation(RgbCapture->GetComponentLocation(), RgbCapture->GetComponentRotation());
			RgbCapture->DestroyComponent();
			RgbCapture = Fresh;
		}
		RgbCapture->CaptureScene();
	}
	DepthCapture->CaptureScene();
	RenderSeconds += FPlatformTime::Seconds() - T0;

	T0 = FPlatformTime::Seconds();
	TArray<uint8> RgbBytes, DepthBytes;
	TArray<FReadbackRequest> Reqs;
	Reqs.Add({DepthTarget->GameThread_GetRenderTargetResource(), 4, &DepthBytes});
	if (bWriteRgb)
	{
		Reqs.Add({RgbTarget->GameThread_GetRenderTargetResource(), 4, &RgbBytes, RgbWidth, RgbHeight});
	}
	const bool bReadOk = ReadTargets(Reqs, Width, Height);
	if (bReadOk && bWriteRgb && SuperSample > 1)
	{
		// SS x SS box filter, per channel, in 8-bit sRGB. Averaging encoded values is not
		// radiometrically exact but it is what every viewer's downscale does, and the point here is
		// stability, not physics.
		const int32 SS = SuperSample;
		TArray<uint8> Small;
		Small.SetNumUninitialized(Width * Height * 4);
		const uint8* Src = RgbBytes.GetData();
		const int32 N = SS * SS;
		for (int32 y = 0; y < Height; ++y)
		{
			for (int32 x = 0; x < Width; ++x)
			{
				uint32 Acc[4] = {0, 0, 0, 0};
				for (int32 dy = 0; dy < SS; ++dy)
				{
					const uint8* Row = Src + ((static_cast<int64>(y) * SS + dy) * RgbWidth + static_cast<int64>(x) * SS) * 4;
					for (int32 dx = 0; dx < SS; ++dx)
					{
						Acc[0] += Row[dx * 4 + 0]; Acc[1] += Row[dx * 4 + 1];
						Acc[2] += Row[dx * 4 + 2]; Acc[3] += Row[dx * 4 + 3];
					}
				}
				uint8* Dst = Small.GetData() + (static_cast<int64>(y) * Width + x) * 4;
				Dst[0] = static_cast<uint8>((Acc[0] + N / 2) / N); Dst[1] = static_cast<uint8>((Acc[1] + N / 2) / N);
				Dst[2] = static_cast<uint8>((Acc[2] + N / 2) / N); Dst[3] = 255;
			}
		}
		RgbBytes = MoveTemp(Small);
	}
	ReadbackSeconds += FPlatformTime::Seconds() - T0;

	if (!bReadOk)
	{
		Finish(FString::Printf(
			TEXT("readback failed at frame %d - stopping rather than writing a trajectory with a "
			     "hole in a P0 channel"), F));
		return;
	}

	T0 = FPlatformTime::Seconds();

	// --- depth: centimetres in, metres out, -1 for sky and beyond range ---
	const float* Src = reinterpret_cast<const float*>(DepthBytes.GetData());
	const int32 N = Width * Height;
	const float MaxCm = DepthMaxRangeM * 100.f;
	int32 Invalid = 0;
	float MinM = TNumericLimits<float>::Max();
	float MaxM = 0.f;
	TArray<FLinearColor> Lin;
	Lin.SetNumUninitialized(N);
	for (int32 i = 0; i < N; ++i)
	{
		const float Cm = Src[i];
		const bool bBad = !FMath::IsFinite(Cm) || Cm <= 0.f || Cm >= MaxCm;
		if (bBad)
		{
			++Invalid;
		}
		else
		{
			const float M = Cm * 0.01f;
			MinM = FMath::Min(MinM, M);
			MaxM = FMath::Max(MaxM, M);
		}
		Lin[i] = FLinearColor(bBad ? -1.f : Cm * 0.01f, 0.f, 0.f, 1.f);
	}
	const float CentreM = Lin[(Height / 2) * Width + (Width / 2)].R;

	if (Invalid == N)
	{
		Finish(FString::Printf(
			TEXT("frame %d: every depth pixel invalid - refusing to write a depth channel that "
			     "contains no depth"), F));
		return;
	}
	// Encoding and file writing go to the thread pool; the readback above stays blocking because
	// that is what ties these pixels to this simulation step.
	EnqueueWrite(F, MoveTemp(RgbBytes), MoveTemp(Lin));

	// --- state, read from the components themselves rather than echoed from the request ---
	const FVector Loc = RgbCapture->GetComponentLocation();
	const FRotator Rot = RgbCapture->GetComponentRotation();
	const FQuat Q = Rot.Quaternion();
	StateLines.Add(FString::Printf(
		TEXT("{\"frame_id\": %d, \"phase\": \"%s\", ")
		TEXT("\"desired_location\": [%.4f, %.4f, %.4f], ")
		TEXT("\"actual_location\": [%.4f, %.4f, %.4f], ")
		TEXT("\"desired_rotation_pyr\": [%.4f, %.4f, %.4f], ")
		TEXT("\"actual_rotation_pyr\": [%.4f, %.4f, %.4f], ")
		TEXT("\"quat_xyzw\": [%.8f, %.8f, %.8f, %.8f], ")
		TEXT("\"depth\": {\"valid_fraction\": %.6f, \"min_m\": %.4f, \"max_m\": %.4f, ")
		TEXT("\"centre_m\": %.4f, \"bit_depth\": %d}}"),
		F, *Esc(P.Phase),
		P.Location.X, P.Location.Y, P.Location.Z, Loc.X, Loc.Y, Loc.Z,
		P.Rotation.Pitch, P.Rotation.Yaw, P.Rotation.Roll, Rot.Pitch, Rot.Yaw, Rot.Roll,
		Q.X, Q.Y, Q.Z, Q.W,
		1.0 - (double)Invalid / N, MaxM > 0.f ? MinM : -1.f, MaxM, CentreM,
		bDepthFloat16 ? 16 : 32));

	WriteSeconds += FPlatformTime::Seconds() - T0;
	++FrameIndex;

	if ((F % 200) == 0)
	{
		const double El = FPlatformTime::Seconds() - StartedSeconds;
		UE_LOG(LogSimWorldCap, Display,
			TEXT("frame %d/%d  %.0fs  %.2f fps  render %.0f ms  readback %.0f ms  write %.0f ms"),
			F, Poses.Num(), El, (F + 1) / FMath::Max(El, 1e-6),
			RenderSeconds / (F + 1) * 1000.0, ReadbackSeconds / (F + 1) * 1000.0,
			WriteSeconds / (F + 1) * 1000.0);
	}
}

void ASimWorldCaptureActor::WaitForSlot()
{
	// Sleeping the game thread here is the bound working as intended: it means encoding cannot
	// keep up, and the alternative is unbounded memory growth.
	while (PendingWrites.GetValue() >= MaxPendingWrites)
	{
		FPlatformProcess::Sleep(0.001f);
	}
}

void ASimWorldCaptureActor::EnqueueWrite(int32 Frame, TArray<uint8>&& RgbBytes,
	TArray<FLinearColor>&& Depth)
{
	WaitForSlot();
	PendingWrites.Increment();

	const FString DepthPath =
		FPaths::Combine(OutputDir, TEXT("depth"), FString::Printf(TEXT("%06d.exr"), Frame));
	const FString RgbPath =
		FPaths::Combine(OutputDir, TEXT("rgb"), FString::Printf(TEXT("%06d.jpg"), Frame));
	const bool bF16 = bDepthFloat16;
	const int32 W = Width, Hh = Height, Q = JpegQuality;
	const bool bRgb = bWriteRgb;
	IImageWrapperModule* Mod = WrapperModule;

	Async(EAsyncExecution::ThreadPool,
		[this, Frame, Rgb = MoveTemp(RgbBytes), Lin = MoveTemp(Depth), DepthPath, RgbPath,
		 bF16, W, Hh, Q, bRgb, Mod]()
		{
			bool bOk = true;

			TArray64<uint8> DepthFile;
			if (bF16)
			{
				// PITFALL - for EXR the pixel type comes from the SOURCE ERawImageFormat, not from
				// the quality argument. RGBA32F with a "float16" flag writes float32: 4x the bytes
				// and a metadata claim nothing would catch, because a float32 EXR is a valid EXR.
				TArray<FFloat16Color> Half;
				Half.SetNumUninitialized(Lin.Num());
				for (int32 i = 0; i < Lin.Num(); ++i)
				{
					Half[i] = FFloat16Color(Lin[i]);
				}
				FImageView V(Half.GetData(), W, Hh, ERawImageFormat::RGBA16F);
				bOk &= FImageUtils::CompressImage(DepthFile, TEXT("exr"), V, 0);
			}
			else
			{
				FImageView V(const_cast<FLinearColor*>(Lin.GetData()), W, Hh,
					ERawImageFormat::RGBA32F);
				bOk &= FImageUtils::CompressImage(DepthFile, TEXT("exr"), V, 0);
			}
			bOk &= DepthFile.Num() > 0 && FFileHelper::SaveArrayToFile(DepthFile, *DepthPath);

			if (bRgb && bOk && Mod)
			{
				// JPEG rather than PNG: PNG is ~5x the bytes and at q92 the artefacts are well
				// below what would change what a model learns. A label channel would have to be
				// PNG - lossy coding of stencil 40 beside 41 invents a 40.3 that decodes to an
				// actor that is not there - but colour is not a label.
				TSharedPtr<IImageWrapper> Wrapper = Mod->CreateImageWrapper(EImageFormat::JPEG);
				bool bJ = Wrapper.IsValid() && Wrapper->SetRaw(Rgb.GetData(), Rgb.Num(), W, Hh,
					ERGBFormat::BGRA, 8);
				if (bJ)
				{
					const TArray64<uint8>& Enc = Wrapper->GetCompressed(Q);
					bJ = Enc.Num() > 0 && FFileHelper::SaveArrayToFile(Enc, *RgbPath);
				}
				bOk &= bJ;
			}

			if (!bOk)
			{
				WriteFailures.Increment();
				UE_LOG(LogSimWorldCap, Error, TEXT("frame %d: encode or write failed"), Frame);
			}
			PendingWrites.Decrement();
		});
}

void ASimWorldCaptureActor::DrainAll()
{
	const double T0 = FPlatformTime::Seconds();
	while (PendingWrites.GetValue() > 0)
	{
		FPlatformProcess::Sleep(0.002f);
	}
	UE_LOG(LogSimWorldCap, Display, TEXT("drained the write queue in %.1fs (%d failures)"),
		FPlatformTime::Seconds() - T0, WriteFailures.GetValue());
}

void ASimWorldCaptureActor::Finish(const FString& Why)
{
	if (bFinished)
	{
		return;
	}
	bFinished = true;
	FinishReason = Why;
	// Drain before declaring the episode done: a states file that lists frames whose images are
	// still in a queue would be a manifest of files that do not exist yet.
	DrainAll();
	if (WriteFailures.GetValue() > 0)
	{
		FinishReason += FString::Printf(TEXT(" (WARNING: %d frames failed to encode or write)"),
			WriteFailures.GetValue());
	}
	FFileHelper::SaveStringToFile(FString::Join(StateLines, TEXT("\n")) + TEXT("\n"), *StatesPath);
	UE_LOG(LogSimWorldCap, Display, TEXT("finished after %d frames: %s"), FrameIndex, *Why);
}

FString ASimWorldCaptureActor::StatusJson() const
{
	const double El = FPlatformTime::Seconds() - StartedSeconds;
	const int32 Done = FMath::Max(FrameIndex, 1);
	return FString::Printf(
		TEXT("{\"ok\": true, \"armed\": %s, \"finished\": %s, \"frame\": %d, \"total\": %d, ")
		TEXT("\"elapsed_s\": %.2f, \"fps\": %.3f, \"render_ms\": %.2f, \"readback_ms\": %.2f, ")
		TEXT("\"write_ms\": %.2f, \"pending_writes\": %d, \"write_failures\": %d, ")
		TEXT("\"walker\": %s, \"walk_anim_length_s\": %.4f, \"walk_root_motion_cm\": %.2f, ")
		TEXT("\"walk_anim_speed_cm_s\": %.2f, \"walker_parts\": %d, ")
		TEXT("\"exposure_manual\": %s, \"exposure_bias_ev\": %.2f, \"auto_exposure_speed\": %.1f, ")
		TEXT("\"local_exposure_shadow\": %.2f, \"local_exposure_highlight\": %.2f, ")
		TEXT("\"history_mode\": %d, \"temporal_aa_enabled\": %s, \"warmup_frames\": %d, ")
		TEXT("\"rgb_render_width\": %d, \"rgb_render_height\": %d, ")
		TEXT("\"states\": \"%s\", \"reason\": \"%s\"}"),
		bArmed ? TEXT("true") : TEXT("false"), bFinished ? TEXT("true") : TEXT("false"),
		FrameIndex, Poses.Num(), El, FrameIndex / FMath::Max(El, 1e-6),
		RenderSeconds / Done * 1000.0, ReadbackSeconds / Done * 1000.0,
		WriteSeconds / Done * 1000.0, PendingWrites.GetValue(), WriteFailures.GetValue(),
		bWalkerMode ? TEXT("true") : TEXT("false"), WalkAnimLengthS, RootMotionCm, AnimSpeedCmS,
		WalkerParts.Num(), bManualExposure ? TEXT("true") : TEXT("false"), ExposureBiasEV, AutoExposureSpeed,
		LocalExposureShadow, LocalExposureHighlight,
		HistoryMode, (RgbCapture && RgbCapture->ShowFlags.TemporalAA) ? TEXT("true") : TEXT("false"),
		HistoryMode == 4 ? WarmupDone : 0, RgbWidth, RgbHeight,
		*Esc(StatesPath), *Esc(FinishReason));
}
