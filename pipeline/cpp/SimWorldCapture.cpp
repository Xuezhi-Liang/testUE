#include "SimWorldCapture.h"

#include "SimWorldCaptureActor.h"

#include "Builders/CubeBuilder.h"
#include "Components/BrushComponent.h"
#include "Components/DirectionalLightComponent.h"
#include "Components/ExponentialHeightFogComponent.h"
#include "Components/LightComponent.h"
#include "Components/SceneCaptureComponent2D.h"
#include "Components/SkyAtmosphereComponent.h"
#include "Components/SkyLightComponent.h"
#include "Engine/DirectionalLight.h"
#include "Engine/ExponentialHeightFog.h"
#include "Engine/Polys.h"
#include "Engine/SkyLight.h"
#include "Engine/TextureRenderTarget2D.h"
#include "Engine/World.h"
#include "EngineUtils.h"
#include "IImageWrapper.h"
#include "IImageWrapperModule.h"
#include "ImageUtils.h"
#include "Math/Float16Color.h"
#include "Modules/ModuleManager.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Model.h"
#include "NavMesh/NavMeshBoundsVolume.h"
#include "NavMesh/RecastNavMesh.h"
#include "NavigationSystem.h"
#include "NavigationPath.h"
#include "RHIGPUReadback.h"

DEFINE_LOG_CATEGORY_STATIC(LogSimWorldCapture, Log, All);

namespace
{
	/**
	 * Read an R32F render target back as true float32.
	 *
	 * PITFALL - do not replace this with FRenderTarget::ReadLinearColorPixels. On Vulkan that
	 * function is not a float path: it calls the FColor overload and widens the result, so it
	 * quantises to 8 bits. For R32F it does not even get that far, because the format is absent
	 * from the RHI's convert-to-FColor switch and it fires a checkf, which on a headless node
	 * surfaces as SIGSEGV inside RHIReadSurfaceData. Advice online recommending
	 * ReadLinearColorPixels for R32F is D3D advice.
	 */
	bool ReadDepthFloat32(FTextureRenderTargetResource* Resource, int32 Width, int32 Height,
		TArray<float>& Out)
	{
		if (!Resource || Width <= 0 || Height <= 0)
		{
			return false;
		}

		Out.SetNumUninitialized(Width * Height);
		bool bOk = false;
		TArray<float>* OutPtr = &Out;
		bool* OkPtr = &bOk;

		ENQUEUE_RENDER_COMMAND(SimWorldDepthReadback)(
			[Resource, Width, Height, OutPtr, OkPtr](FRHICommandListImmediate& RHICmdList)
			{
				FRHITexture* Texture = Resource->GetRenderTargetTexture();
				if (!Texture)
				{
					return;
				}

				FRHIGPUTextureReadback Readback(TEXT("SimWorldDepthReadback"));
				// PITFALL - explicit origin and size, not the defaulted whole-texture overload.
				// The defaulted FResolveRect is empty and an empty rect is not reliably treated
				// as "everything" across RHIs. Symptom: depth reads back as all zeros, EXRs are
				// written, frame counts match, nothing errors.
				Readback.EnqueueCopy(RHICmdList, Texture, FIntVector::ZeroValue, 0,
					FIntVector(Width, Height, 1));
				// Capture is not wall-clock bound here, so blocking is the cheap way to
				// guarantee these pixels belong to this pose rather than to whatever the GPU
				// last finished.
				RHICmdList.BlockUntilGPUIdle();

				int32 RowPitchInPixels = 0;
				const float* Src =
					static_cast<const float*>(Readback.Lock(RowPitchInPixels, nullptr));
				if (!Src)
				{
					return;
				}
				// PITFALL - row pitch is in pixels and is usually padded past Width. Indexing by
				// Width shears the image progressively down the frame.
				for (int32 Y = 0; Y < Height; ++Y)
				{
					const float* Row = Src + static_cast<int64>(Y) * RowPitchInPixels;
					for (int32 X = 0; X < Width; ++X)
					{
						(*OutPtr)[Y * Width + X] = Row[X];
					}
				}
				Readback.Unlock();
				*OkPtr = true;
			});

		FlushRenderingCommands();
		return bOk;
	}

	FString Esc(const FString& In)
	{
		return In.Replace(TEXT("\\"), TEXT("\\\\")).Replace(TEXT("\""), TEXT("\\\""));
	}

	FString Fail(const FString& Why)
	{
		return FString::Printf(TEXT("{\"ok\": false, \"error\": \"%s\"}"), *Esc(Why));
	}
}

FString USimWorldCapture::CaptureDepthEXR(UObject* WorldContextObject, FVector Location,
	FRotator Rotation, float FOVDegrees, int32 Width, int32 Height, const FString& OutPath,
	float MaxRangeM, bool bFloat16)
{
	UWorld* World = GEngine ? GEngine->GetWorldFromContextObject(
		WorldContextObject, EGetWorldErrorMode::ReturnNull) : nullptr;
	if (!World)
	{
		return Fail(TEXT("no world"));
	}

	UTextureRenderTarget2D* Target = NewObject<UTextureRenderTarget2D>(World);
	// R32f is required: SCS_SceneDepth writes linear centimetres, which an 8-bit target would
	// quantise into uselessness.
	Target->RenderTargetFormat = RTF_R32f;
	Target->ClearColor = FLinearColor::Black;
	Target->bAutoGenerateMips = false;
	Target->InitAutoFormat(Width, Height);
	Target->UpdateResourceImmediate(true);

	AActor* Holder = World->SpawnActor<AActor>(AActor::StaticClass(), Location, Rotation);
	if (!Holder)
	{
		return Fail(TEXT("could not spawn the capture holder"));
	}
	// PITFALL - a bare AActor spawned like this has NO root component, so attaching to
	// GetRootComponent() attaches to nullptr and the capture's world transform is never the one
	// asked for. It still renders, from somewhere else, and the depth that comes back is
	// perfectly well-formed and completely wrong: measured 2.2-19.9 m of plausible geometry
	// while the centre pixel read -1 against a line trace that said 4.22 m.
	USceneComponent* Root = NewObject<USceneComponent>(Holder, TEXT("SimWorldCaptureRoot"));
	Holder->SetRootComponent(Root);
	Root->RegisterComponent();
	Root->SetWorldLocationAndRotation(Location, Rotation);

	USceneCaptureComponent2D* Capture =
		NewObject<USceneCaptureComponent2D>(Holder, TEXT("SimWorldDepthCapture"));
	Capture->SetupAttachment(Root);
	Capture->RegisterComponent();
	Capture->TextureTarget = Target;
	Capture->CaptureSource = SCS_SceneDepth;
	// Manual capture only: an automatic per-frame capture fires at a point in the frame we do
	// not control, which is how pixels and recorded pose drift apart.
	Capture->bCaptureEveryFrame = false;
	Capture->bCaptureOnMovement = false;
	// PITFALL - required even though nothing here wants temporal history:
	// UMaterialInterface::OverrideBlendableSettings returns immediately when the view has no
	// FSceneViewState, and several capture paths silently degrade without one.
	Capture->bAlwaysPersistRenderingState = true;
	Capture->FOVAngle = FOVDegrees;
	Capture->ProjectionType = ECameraProjectionMode::Perspective;
	Capture->ShowFlags.SetAntiAliasing(false);
	Capture->ShowFlags.SetMotionBlur(false);
	// PITFALL - bRenderInMainRenderer looks like free performance (it folds the depth capture
	// into the main renderer instead of running a second scene render) but it needs a main
	// renderer view to attach to. Headless with only scene captures there is none, and the
	// target comes back all zeros: valid-looking EXRs containing no depth at all.
	Capture->bRenderInMainRenderer = false;
	Capture->SetWorldLocationAndRotation(Location, Rotation);
	Capture->CaptureScene();

	TArray<float> Raw;
	const bool bRead = ReadDepthFloat32(Target->GameThread_GetRenderTargetResource(),
		Width, Height, Raw);
	Holder->Destroy();
	if (!bRead || Raw.Num() != Width * Height)
	{
		return Fail(TEXT("readback failed; wrote nothing rather than an empty EXR"));
	}

	// centimetres -> metres, with the sentinel applied once, here.
	const float MaxCm = MaxRangeM * 100.f;
	TArray<FLinearColor> Pixels;
	Pixels.SetNumUninitialized(Width * Height);
	int32 Invalid = 0;
	for (int32 i = 0; i < Raw.Num(); ++i)
	{
		const float Cm = Raw[i];
		const bool bBad = !FMath::IsFinite(Cm) || Cm <= 0.f || Cm >= MaxCm;
		Invalid += bBad ? 1 : 0;
		Pixels[i] = FLinearColor(bBad ? -1.f : Cm / 100.f, 0.f, 0.f, 1.f);
	}

	// If every pixel is invalid the capture did not work, whatever the readback said. Writing
	// the file anyway is how a dataset acquires a depth channel that contains no depth.
	if (Invalid == Raw.Num())
	{
		return Fail(FString::Printf(
			TEXT("all %d pixels invalid - refused to write %s"), Invalid, *OutPath));
	}

	// PITFALL - for EXR the pixel type comes from the SOURCE ERawImageFormat, not from the
	// quality argument. Passing RGBA32F and a "float16" flag wrote float32 while the metadata
	// claimed 16-bit: 1.33 MB per frame instead of ~0.33 MB, and a false statement in
	// camera.json. float16 is genuinely enough here - its precision is relative (~5e-4), so in
	// metres it gives 1 mm at 1 m and 6 cm at 100 m, and UE5-Agent-Data measured float16 and
	// float32 producing an identical invalid fraction and identical max valid depth.
	TArray64<uint8> Buffer;
	bool bCompressed = false;
	TArray<FFloat16Color> Half;
	if (bFloat16)
	{
		Half.SetNumUninitialized(Pixels.Num());
		for (int32 i = 0; i < Pixels.Num(); ++i)
		{
			Half[i] = FFloat16Color(Pixels[i]);
		}
		FImageView View16(Half.GetData(), Width, Height, ERawImageFormat::RGBA16F);
		bCompressed = FImageUtils::CompressImage(Buffer, TEXT("exr"), View16, 0);
	}
	else
	{
		FImageView View32(Pixels.GetData(), Width, Height, ERawImageFormat::RGBA32F);
		bCompressed = FImageUtils::CompressImage(Buffer, TEXT("exr"), View32, 0);
	}
	if (!bCompressed)
	{
		return Fail(TEXT("EXR compress failed"));
	}
	IFileManager::Get().MakeDirectory(*FPaths::GetPath(OutPath), true);
	if (!FFileHelper::SaveArrayToFile(Buffer, *OutPath))
	{
		return Fail(FString::Printf(TEXT("could not write %s"), *OutPath));
	}

	// Statistics measured here, so a caller can check the depth without trusting its own EXR
	// reader. Ours was an opencv build with `OpenEXR: NO`, which made a good file read as None.
	float MinM = TNumericLimits<float>::Max();
	float MaxM = 0.f;
	for (const FLinearColor& P : Pixels)
	{
		if (P.R > 0.f)
		{
			MinM = FMath::Min(MinM, P.R);
			MaxM = FMath::Max(MaxM, P.R);
		}
	}
	const float CentreM = Pixels[(Height / 2) * Width + (Width / 2)].R;
	// The component's own world transform, read back rather than echoed from the argument: a
	// bare AActor has no root component, and attaching to a null root leaves the capture
	// somewhere else entirely while still producing plausible depth.
	const FVector ActualLoc = Capture->GetComponentLocation();
	const FRotator ActualRot = Capture->GetComponentRotation();

	UE_LOG(LogSimWorldCapture, Display,
		TEXT("CaptureDepthEXR: %s  %dx%d  invalid %.1f%%  centre %.3f m  at %s / %s"),
		*OutPath, Width, Height, 100.f * Invalid / Raw.Num(), CentreM,
		*ActualLoc.ToString(), *ActualRot.ToString());

	return FString::Printf(
		TEXT("{\"ok\": true, \"path\": \"%s\", \"width\": %d, \"height\": %d, ")
		TEXT("\"invalid_fraction\": %.6f, \"min_m\": %.4f, \"max_m\": %.4f, ")
		TEXT("\"centre_m\": %.4f, \"requested_location\": [%.3f, %.3f, %.3f], ")
		TEXT("\"actual_location\": [%.3f, %.3f, %.3f], ")
		TEXT("\"requested_rotation_pyr\": [%.3f, %.3f, %.3f], ")
		TEXT("\"actual_rotation_pyr\": [%.3f, %.3f, %.3f], ")
		TEXT("\"unit\": \"metres\", \"channel\": \"R (index 2 via OpenCV BGRA)\", ")
		TEXT("\"invalid_value\": -1.0, \"max_range_m\": %.1f, \"bit_depth\": %d, ")
		TEXT("\"bytes\": %lld, \"float16\": %s}"),
		*Esc(OutPath), Width, Height, (double)Invalid / Raw.Num(),
		MaxM > 0.f ? MinM : -1.f, MaxM, CentreM,
		Location.X, Location.Y, Location.Z, ActualLoc.X, ActualLoc.Y, ActualLoc.Z,
		Rotation.Pitch, Rotation.Yaw, Rotation.Roll,
		ActualRot.Pitch, ActualRot.Yaw, ActualRot.Roll,
		MaxRangeM, bFloat16 ? 16 : 32, (long long)Buffer.Num(),
		bFloat16 ? TEXT("true") : TEXT("false"));
}

FString USimWorldCapture::SpawnLightingRig(UObject* WorldContextObject, float SunIntensity,
	float SunPitch, float SunYaw, float SunSourceAngle, float SkyIntensity, float FogDensity)
{
	UWorld* World = GEngine ? GEngine->GetWorldFromContextObject(
		WorldContextObject, EGetWorldErrorMode::ReturnNull) : nullptr;
	if (!World)
	{
		return Fail(TEXT("no world"));
	}
	const FName Tag(TEXT("SimWorldRig"));
	auto Label = [&](AActor* A, const TCHAR* Name)
	{
		A->Tags.Add(Tag);
#if WITH_EDITOR
		A->SetActorLabel(Name);
#endif
	};

	ADirectionalLight* Sun = World->SpawnActor<ADirectionalLight>(ADirectionalLight::StaticClass(),
		FVector(0.f, 0.f, 60000.f), FRotator(SunPitch, SunYaw, 0.f));
	if (!Sun || !Sun->GetComponent())
	{
		return Fail(TEXT("could not spawn the directional light"));
	}
	UDirectionalLightComponent* SC = Sun->GetComponent();
	SC->SetMobility(EComponentMobility::Movable);
	SC->SetIntensity(SunIntensity);
	SC->SetAtmosphereSunLight(true);
	SC->SetLightSourceAngle(SunSourceAngle);
	Label(Sun, TEXT("SimWorldRig_Sun"));

	// There is no ASkyAtmosphere actor class in this engine's Classes/; the component on a plain
	// actor is what the editor's own placement does under the hood.
	AActor* Atm = World->SpawnActor<AActor>(AActor::StaticClass(), FVector::ZeroVector, FRotator::ZeroRotator);
	if (!Atm)
	{
		return Fail(TEXT("could not spawn the atmosphere holder"));
	}
	USceneComponent* AtmRoot = NewObject<USceneComponent>(Atm, TEXT("Root"));
	Atm->SetRootComponent(AtmRoot);
	AtmRoot->RegisterComponent();
	USkyAtmosphereComponent* AtmC = NewObject<USkyAtmosphereComponent>(Atm, TEXT("SkyAtmosphere"));
	AtmC->SetupAttachment(AtmRoot);
	AtmC->RegisterComponent();
	Label(Atm, TEXT("SimWorldRig_Atmosphere"));

	ASkyLight* Sky = World->SpawnActor<ASkyLight>(ASkyLight::StaticClass(), FVector::ZeroVector, FRotator::ZeroRotator);
	if (!Sky || !Sky->GetLightComponent())
	{
		return Fail(TEXT("could not spawn the sky light"));
	}
	USkyLightComponent* KC = Sky->GetLightComponent();
	KC->SetMobility(EComponentMobility::Movable);
	KC->SetIntensity(SkyIntensity);
	KC->SetRealTimeCapture(true);
	KC->RecaptureSky();
	Label(Sky, TEXT("SimWorldRig_SkyLight"));

	AExponentialHeightFog* Fog = World->SpawnActor<AExponentialHeightFog>(AExponentialHeightFog::StaticClass(),
		FVector::ZeroVector, FRotator::ZeroRotator);
	if (!Fog || !Fog->GetComponent())
	{
		return Fail(TEXT("could not spawn the height fog"));
	}
	Fog->GetComponent()->SetFogDensity(FogDensity);
	Label(Fog, TEXT("SimWorldRig_Haze"));

	return FString::Printf(
		TEXT("{\"ok\": true, \"sun\": \"%s\", \"atmosphere\": \"%s\", \"sky\": \"%s\", \"fog\": \"%s\", ")
		TEXT("\"sun_intensity\": %.3f, \"sun_pitch\": %.2f, \"sun_yaw\": %.2f, \"sun_source_angle\": %.2f, ")
		TEXT("\"sky_intensity\": %.3f, \"fog_density\": %.5f}"),
		*Esc(Sun->GetName()), *Esc(Atm->GetName()), *Esc(Sky->GetName()), *Esc(Fog->GetName()),
		SunIntensity, SunPitch, SunYaw, SunSourceAngle, SkyIntensity, FogDensity);
}

FString USimWorldCapture::SpawnSkyLight(UObject* WorldContextObject, float Intensity, bool bRealTimeCapture)
{
	UWorld* World = GEngine ? GEngine->GetWorldFromContextObject(
		WorldContextObject, EGetWorldErrorMode::ReturnNull) : nullptr;
	if (!World)
	{
		return Fail(TEXT("no world"));
	}
	ASkyLight* Sky = World->SpawnActor<ASkyLight>(ASkyLight::StaticClass(), FVector::ZeroVector, FRotator::ZeroRotator);
	if (!Sky || !Sky->GetLightComponent())
	{
		return Fail(TEXT("could not spawn the sky light"));
	}
	USkyLightComponent* KC = Sky->GetLightComponent();
	KC->SetMobility(EComponentMobility::Movable);
	KC->SetIntensity(Intensity);
	// Real-time capture renders ONLY the sky atmosphere, volumetric clouds and height fog into the
	// cubemap. A purchased level's sky is usually a painted sphere mesh with no atmosphere, so a
	// real-time-capture sky light there captures black and its intensity changes nothing: x1, x3
	// and x6 rendered identical frames on Downtown West. Captured-scene mode photographs whatever
	// is beyond SkyDistanceThreshold - the painted sky included - once, on RecaptureSky().
	KC->SourceType = SLS_CapturedScene;
	KC->SetRealTimeCapture(bRealTimeCapture);
	KC->RecaptureSky();
	KC->MarkRenderStateDirty();
	Sky->Tags.Add(FName(TEXT("SimWorldFillSky")));
#if WITH_EDITOR
	Sky->SetActorLabel(TEXT("SimWorldFillSky"));
#endif
	return FString::Printf(TEXT("{\"ok\": true, \"actor\": \"%s\", \"intensity\": %.4f}"),
		*Esc(Sky->GetName()), Intensity);
}

FString USimWorldCapture::SetLightIntensity(UObject* WorldContextObject, const FString& ActorName, float Intensity)
{
	UWorld* World = GEngine ? GEngine->GetWorldFromContextObject(
		WorldContextObject, EGetWorldErrorMode::ReturnNull) : nullptr;
	if (!World)
	{
		return Fail(TEXT("no world"));
	}
	int32 Set = 0;
	for (TActorIterator<AActor> It(World); It; ++It)
	{
		if (It->GetName() != ActorName)
		{
			continue;
		}
		TArray<ULightComponentBase*> Comps;
		It->GetComponents<ULightComponentBase>(Comps);
		for (ULightComponentBase* C : Comps)
		{
			// SetIntensity lives on the two subclasses, not the base (LightComponent.h:286,
			// SkyLightComponent.h:235 - read, not guessed, after the base-class call failed to compile).
			if (USkyLightComponent* SC = Cast<USkyLightComponent>(C))
			{
				SC->SetIntensity(Intensity);
				SC->RecaptureSky();
			}
			else if (ULightComponent* LC = Cast<ULightComponent>(C))
			{
				LC->SetIntensity(Intensity);
			}
			else
			{
				continue;
			}
			C->MarkRenderStateDirty();
			++Set;
		}
	}
	if (Set == 0)
	{
		return Fail(FString::Printf(TEXT("no light component on an actor named %s"), *ActorName));
	}
	return FString::Printf(TEXT("{\"ok\": true, \"actor\": \"%s\", \"components\": %d, \"intensity\": %.4f}"),
		*Esc(ActorName), Set, Intensity);
}

FString USimWorldCapture::CaptureRgbPng(UObject* WorldContextObject, FVector Location,
	FRotator Rotation, float FOVDegrees, int32 Width, int32 Height, const FString& OutPath,
	float ExposureBiasEV, bool bManualExposure, float LocalExposureShadow, float LocalExposureHighlight,
	float LocalExposureDetail)
{
	UWorld* World = GEngine ? GEngine->GetWorldFromContextObject(
		WorldContextObject, EGetWorldErrorMode::ReturnNull) : nullptr;
	if (!World)
	{
		return Fail(TEXT("no world"));
	}

	UTextureRenderTarget2D* Target = NewObject<UTextureRenderTarget2D>(World);
	Target->RenderTargetFormat = RTF_RGBA8_SRGB;
	Target->ClearColor = FLinearColor::Black;
	Target->bAutoGenerateMips = false;
	Target->InitAutoFormat(Width, Height);
	Target->UpdateResourceImmediate(true);

	AActor* Holder = World->SpawnActor<AActor>(AActor::StaticClass(), Location, Rotation);
	if (!Holder)
	{
		return Fail(TEXT("could not spawn the capture holder"));
	}
	// Same root-component pitfall as CaptureDepthEXR: a bare AActor has none, and a capture
	// attached to nullptr renders from somewhere else.
	USceneComponent* Root = NewObject<USceneComponent>(Holder, TEXT("SimWorldRgbRoot"));
	Holder->SetRootComponent(Root);
	Root->RegisterComponent();
	Root->SetWorldLocationAndRotation(Location, Rotation);

	USceneCaptureComponent2D* Capture =
		NewObject<USceneCaptureComponent2D>(Holder, TEXT("SimWorldRgbProbe"));
	Capture->SetupAttachment(Root);
	Capture->RegisterComponent();
	Capture->TextureTarget = Target;
	Capture->CaptureSource = SCS_FinalColorLDR;
	Capture->bCaptureEveryFrame = false;
	Capture->bCaptureOnMovement = false;
	Capture->bAlwaysPersistRenderingState = true;
	Capture->FOVAngle = FOVDegrees;
	Capture->ProjectionType = ECameraProjectionMode::Perspective;
	Capture->bRenderInMainRenderer = false;
	if (bManualExposure)
	{
		FPostProcessSettings& PP = Capture->PostProcessSettings;
		PP.bOverride_AutoExposureMethod = true;
		PP.AutoExposureMethod = AEM_Manual;
		PP.bOverride_AutoExposureBias = true;
		PP.AutoExposureBias = ExposureBiasEV;
		Capture->PostProcessBlendWeight = 1.f;
	}
	if (LocalExposureShadow > 0.f || LocalExposureHighlight > 0.f)
	{
		FPostProcessSettings& PP = Capture->PostProcessSettings;
		PP.bOverride_LocalExposureMethod = true;
		PP.LocalExposureMethod = ELocalExposureMethod::Bilateral;
		if (LocalExposureShadow > 0.f) { PP.bOverride_LocalExposureShadowContrastScale = true; PP.LocalExposureShadowContrastScale = LocalExposureShadow; }
		if (LocalExposureHighlight > 0.f) { PP.bOverride_LocalExposureHighlightContrastScale = true; PP.LocalExposureHighlightContrastScale = LocalExposureHighlight; }
		if (LocalExposureDetail > 0.f) { PP.bOverride_LocalExposureDetailStrength = true; PP.LocalExposureDetailStrength = LocalExposureDetail; }
		Capture->PostProcessBlendWeight = 1.f;
	}
	Capture->SetWorldLocationAndRotation(Location, Rotation);
	Capture->CaptureScene();

	TArray<FColor> Pixels;
	FTextureRenderTargetResource* Res = Target->GameThread_GetRenderTargetResource();
	// ReadPixels is fine HERE: the target is 8-bit sRGB, so the FColor path is exact. It is the
	// float formats where it quantises or fires a checkf (see ReadDepthFloat32).
	const bool bRead = Res && Res->ReadPixels(Pixels);
	Holder->Destroy();
	if (!bRead || Pixels.Num() != Width * Height)
	{
		return Fail(TEXT("rgb readback failed; wrote nothing"));
	}

	// Histogram summary in-engine so a bias sweep can be ranked without decoding PNGs.
	double SumY = 0.0;
	int32 Blown = 0, Black = 0;
	TArray<int32> Hist;
	Hist.SetNumZeroed(256);
	for (const FColor& C : Pixels)
	{
		const int32 Y = FMath::Clamp(FMath::RoundToInt(0.2126f * C.R + 0.7152f * C.G + 0.0722f * C.B), 0, 255);
		SumY += Y;
		Blown += (Y >= 250) ? 1 : 0;
		Black += (Y <= 5) ? 1 : 0;
		Hist[Y]++;
	}
	auto Pct = [&](double Q) -> int32
	{
		const double Want = Q * Pixels.Num();
		double Acc = 0.0;
		for (int32 i = 0; i < 256; ++i)
		{
			Acc += Hist[i];
			if (Acc >= Want) return i;
		}
		return 255;
	};

	IImageWrapperModule& Mod = FModuleManager::LoadModuleChecked<IImageWrapperModule>(TEXT("ImageWrapper"));
	TSharedPtr<IImageWrapper> Wrapper = Mod.CreateImageWrapper(EImageFormat::PNG);
	bool bOk = Wrapper.IsValid() && Wrapper->SetRaw(Pixels.GetData(), Pixels.Num() * sizeof(FColor),
		Width, Height, ERGBFormat::BGRA, 8);
	if (bOk)
	{
		const TArray64<uint8>& Enc = Wrapper->GetCompressed(0);
		bOk = Enc.Num() > 0 && FFileHelper::SaveArrayToFile(Enc, *OutPath);
	}
	if (!bOk)
	{
		return Fail(FString::Printf(TEXT("could not encode or write %s"), *OutPath));
	}
	return FString::Printf(
		TEXT("{\"ok\": true, \"path\": \"%s\", \"manual\": %s, \"bias_ev\": %.2f, ")
		TEXT("\"mean_y\": %.2f, \"p10\": %d, \"p50\": %d, \"p99\": %d, ")
		TEXT("\"blown_frac\": %.5f, \"black_frac\": %.5f}"),
		*Esc(OutPath), bManualExposure ? TEXT("true") : TEXT("false"), ExposureBiasEV,
		SumY / Pixels.Num(), Pct(0.10), Pct(0.50), Pct(0.99),
		double(Blown) / Pixels.Num(), double(Black) / Pixels.Num());
}

FString USimWorldCapture::EnsureNavMesh(UObject* WorldContextObject, FVector RegionCenter,
	FVector RegionExtent, float PaddingCm, float TimeoutSeconds)
{
	bool bWasSynthesised = false;
	FString Note;
	UWorld* World = GEngine ? GEngine->GetWorldFromContextObject(
		WorldContextObject, EGetWorldErrorMode::ReturnNull) : nullptr;
	UNavigationSystemV1* NavSys = World ? UNavigationSystemV1::GetCurrent(World) : nullptr;
	if (!World || !NavSys)
	{
		return Fail(TEXT("no world or no navigation system"));
	}

	// Reuse an existing volume ONLY if it actually covers the region asked about.
	//
	// Checking merely "does the level have a bounds volume" is how a navmesh ends up describing
	// somewhere else. Measured on ModularNeighborhood: a volume left from an earlier call covered
	// +/-2755 cm around the world origin while the requested region was at (-18076, -3606) - 18 km
	// away - so the export came back with 537 triangles of empty ground, points projected onto it
	// successfully, and the resulting anchor sat in open sky. Every downstream number looked fine.
	//
	// UE5-Agent-Data hit the same shape with occupancy grids: a region centred on the origin
	// covered pure void, came out 0% blocked, and forty trajectories were collected against it.
	const FBox Wanted(RegionCenter - RegionExtent, RegionCenter + RegionExtent);
	int32 Existing = 0;
	int32 Covering = 0;
	bool bOursFromEarlier = false;
	for (TActorIterator<ANavMeshBoundsVolume> It(World); It; ++It)
	{
		++Existing;
		const FBox VolBox = It->GetComponentsBoundingBox(true);
		// "Covers" means it contains the centre and overlaps most of the region; a volume that
		// merely clips a corner does not give the agent anywhere to walk.
		if (VolBox.IsInsideOrOn(RegionCenter) && VolBox.Intersect(Wanted))
		{
			++Covering;
		}
		// A volume we synthesised earlier in this session is still in the live level, so a
		// second call would otherwise report it as "authored" and the dataset would record the
		// wrong provenance for a navmesh we invented.
		if (It->GetName().Contains(TEXT("SimWorld_SynthesisedNavBounds")))
		{
			bOursFromEarlier = true;
		}
	}

	if (Covering == 0)
	{
		if (Existing > 0)
		{
			Note = FString::Printf(
				TEXT("the level already had %d NavMeshBoundsVolume(s) but none covered the "
				     "requested region, so another was added for it - a navmesh that describes "
				     "somewhere else is worse than none, because points project onto it"),
				Existing);
			UE_LOG(LogSimWorldCapture, Warning, TEXT("%s"), *Note);
		}
		const FBox Box = FBox(RegionCenter - RegionExtent,
			RegionCenter + RegionExtent).ExpandBy(PaddingCm);
		const FVector Size = Box.GetSize();

		FActorSpawnParameters Params;
		// Unique per region: reusing the name would collide with a volume we made for a different
		// place, which is the situation this whole check exists to catch.
		Params.Name = MakeUniqueObjectName(World, ANavMeshBoundsVolume::StaticClass(),
			TEXT("SimWorld_SynthesisedNavBounds"));
		ANavMeshBoundsVolume* Volume = World->SpawnActor<ANavMeshBoundsVolume>(
			ANavMeshBoundsVolume::StaticClass(), Box.GetCenter(), FRotator::ZeroRotator, Params);
		if (!Volume)
		{
			return Fail(TEXT("could not spawn ANavMeshBoundsVolume"));
		}

		// PITFALL - a volume spawned from code has no brush at all, and scaling an absent brush
		// is a no-op, so the UModel/UPolys pair has to be built by hand before UCubeBuilder has
		// anything to fill in. Skipping this leaves the bounds degenerate and the build covers
		// nothing, with no error anywhere. These three types are also why this cannot be done
		// from Python - none of them is exposed.
		Volume->PreEditChange(nullptr);
		Volume->Brush = NewObject<UModel>(Volume, NAME_None, RF_Transactional);
		Volume->Brush->Initialize(nullptr, true);
		Volume->Brush->Polys = NewObject<UPolys>(Volume->Brush, NAME_None, RF_Transactional);
		Volume->GetBrushComponent()->Brush = Volume->Brush;

		UCubeBuilder* Builder = NewObject<UCubeBuilder>();
		Builder->X = Size.X;
		Builder->Y = Size.Y;
		Builder->Z = Size.Z;
		Builder->Build(World, Volume);
		Volume->PostEditChange();

		// PostRegisterAllComponents already announced this volume, but that happened at spawn
		// time when the brush was empty and the bounds degenerate. Re-announcing with the real
		// brush is what actually registers the area to build.
		NavSys->OnNavigationBoundsUpdated(Volume);
		bWasSynthesised = true;
	}

	NavSys->Build();

	// Build() only marks tiles dirty; nothing is dispatched until the navigation system ticks.
	//
	// UE5-Agent-Data drives those ticks by hand and has to: a commandlet never ticks the world, so
	// the tile jobs would never finish. Copying that into a live editor is wrong. This editor runs
	// in play-simulate mode, so the world already ticks - and calling NavSys->Tick() from a console
	// command that is itself executing inside the engine's tick re-enters the navigation system. It
	// hung WinterTown for 42 minutes at 584% CPU with the log completely silent, no error, no
	// timeout: the loop's own deadline never got a chance to run.
	//
	// So this returns straight after dispatching, and the caller polls NavBuildStatus between
	// console commands. Each command returns promptly and the engine ticks normally in between,
	// which is what lets the build finish at all.
	(void)TimeoutSeconds;
	ARecastNavMesh* Nav = Cast<ARecastNavMesh>(NavSys->GetMainNavData());
	Note = NavSys->IsNavigationBuildInProgress()
		? TEXT("build dispatched and still in progress; poll nav_build_status until in_progress "
		       "is false before querying paths")
		: TEXT("build dispatched; the navigation system already reports idle, which on entry can "
		       "also mean it has not started yet - poll nav_build_status rather than trusting it");
	return FString::Printf(
		TEXT("{\"ok\": true, \"synthesised\": %s, \"bounds_volumes\": %d, ")
		TEXT("\"volumes_covering_region\": %d, ")
		TEXT("\"agent_radius_cm\": %.1f, \"agent_height_cm\": %.1f, ")
		TEXT("\"provenance\": \"%s\", \"note\": \"%s\"}"),
		(bWasSynthesised || bOursFromEarlier) ? TEXT("true") : TEXT("false"),
		FMath::Max(Existing, 1), Covering,
		// Nav can legitimately be null here: the build was only just dispatched. -1 says "not yet"
		// rather than crashing, and NavBuildStatus is where the real answer comes from.
		Nav ? Nav->AgentRadius : -1.f, Nav ? Nav->AgentHeight : -1.f,
		bWasSynthesised
			? TEXT("synthesised by this call and NOT saved into the map; walkable surface outside the region exists in the level but was not generated here")
			: (bOursFromEarlier
				? TEXT("synthesised earlier in this editor session and still live in the level; NOT saved into the map")
				: TEXT("authored: the level ships one or more ANavMeshBoundsVolume")),
		*Esc(Note));
}

FString USimWorldCapture::NavBuildStatus(UObject* WorldContextObject)
{
	UWorld* World = GEngine ? GEngine->GetWorldFromContextObject(
		WorldContextObject, EGetWorldErrorMode::ReturnNull) : nullptr;
	UNavigationSystemV1* NavSys = World ? UNavigationSystemV1::GetCurrent(World) : nullptr;
	if (!NavSys)
	{
		return Fail(TEXT("no navigation system"));
	}
	ARecastNavMesh* Nav = Cast<ARecastNavMesh>(NavSys->GetMainNavData());
	// "not in progress" is not the same as "finished": on entry, before the system has ticked, an
	// unstarted build also reports idle. A caller must see idle AND a navmesh, and is better off
	// seeing it hold for a few polls - which is the same reason the original recipe waited for
	// several consecutive idle ticks.
	return FString::Printf(
		TEXT("{\"ok\": true, \"in_progress\": %s, \"has_navmesh\": %s, ")
		TEXT("\"agent_radius_cm\": %.1f, \"agent_height_cm\": %.1f}"),
		NavSys->IsNavigationBuildInProgress() ? TEXT("true") : TEXT("false"),
		Nav ? TEXT("true") : TEXT("false"),
		Nav ? Nav->AgentRadius : -1.f, Nav ? Nav->AgentHeight : -1.f);
}

FString USimWorldCapture::ProjectToNav(UObject* WorldContextObject, FVector Point,
	FVector QueryExtent)
{
	UWorld* World = GEngine ? GEngine->GetWorldFromContextObject(
		WorldContextObject, EGetWorldErrorMode::ReturnNull) : nullptr;
	UNavigationSystemV1* NavSys = World ? UNavigationSystemV1::GetCurrent(World) : nullptr;
	if (!NavSys)
	{
		return Fail(TEXT("no navigation system"));
	}
	FNavLocation Out;
	if (!NavSys->ProjectPointToNavigation(Point, Out, QueryExtent))
	{
		return Fail(TEXT("point does not project onto the navmesh within the query extent"));
	}
	return FString::Printf(
		TEXT("{\"ok\": true, \"point\": [%.3f, %.3f, %.3f], \"dz_cm\": %.3f}"),
		Out.Location.X, Out.Location.Y, Out.Location.Z, Out.Location.Z - Point.Z);
}

FString USimWorldCapture::FindNavPath(UObject* WorldContextObject, FVector Start, FVector Goal)
{
	UWorld* World = GEngine ? GEngine->GetWorldFromContextObject(
		WorldContextObject, EGetWorldErrorMode::ReturnNull) : nullptr;
	UNavigationSystemV1* NavSys = World ? UNavigationSystemV1::GetCurrent(World) : nullptr;
	if (!NavSys)
	{
		return Fail(TEXT("no navigation system"));
	}
	UNavigationPath* Path = NavSys->FindPathToLocationSynchronously(World, Start, Goal);
	if (!Path || !Path->IsValid())
	{
		return Fail(TEXT("no path: the goal is unreachable for this agent, or either end is off "
		                 "the navmesh"));
	}
	FString Pts;
	double Length = 0.0;
	for (int32 i = 0; i < Path->PathPoints.Num(); ++i)
	{
		const FVector& P = Path->PathPoints[i];
		Pts += FString::Printf(TEXT("%s[%.3f, %.3f, %.3f]"), i ? TEXT(", ") : TEXT(""),
			P.X, P.Y, P.Z);
		if (i)
		{
			Length += FVector::Dist(Path->PathPoints[i - 1], P);
		}
	}
	// A partial path is not a failure, but it must never be mistaken for a complete one: it ends
	// at the closest reachable point, which is exactly where a route would silently stop short.
	return FString::Printf(
		TEXT("{\"ok\": %s, \"partial\": %s, \"point_count\": %d, \"length_cm\": %.1f, ")
		TEXT("\"points\": [%s]}"),
		Path->PathPoints.Num() > 0 ? TEXT("true") : TEXT("false"),
		Path->IsPartial() ? TEXT("true") : TEXT("false"),
		Path->PathPoints.Num(), Length, *Pts);
}

FString USimWorldCapture::ExportNavMesh(UObject* WorldContextObject, const FString& OutBinPath,
	const FString& OutJsonPath)
{
	UWorld* World = GEngine ? GEngine->GetWorldFromContextObject(
		WorldContextObject, EGetWorldErrorMode::ReturnNull) : nullptr;
	UNavigationSystemV1* NavSys = World ? UNavigationSystemV1::GetCurrent(World) : nullptr;
	ARecastNavMesh* Nav = NavSys ? Cast<ARecastNavMesh>(NavSys->GetMainNavData()) : nullptr;
	if (!Nav)
	{
		return Fail(TEXT("no navmesh to export"));
	}

	FRecastDebugGeometry Geo;
	Nav->GetDebugGeometryForTile(Geo, INDEX_NONE);   // INDEX_NONE = every tile
	const TArray<FVector>& Verts = Geo.MeshVerts;

	// AreaIndices is one index array PER AREA TYPE, and slot 0 is RECAST_NULL_AREA - the
	// unwalkable one - so it is legitimately empty. Reading only [0] reported "navmesh present
	// but empty (4604 verts, 0 indices)" for a navmesh that was entirely fine; the walkable
	// triangles were in the other slots. Everything except the null area is collected here, and
	// the per-area counts are reported so a surprise is visible rather than silent.
	TArray<int32> Indices;
	FString AreaCounts;
	for (int32 Area = 1; Area < RECAST_MAX_AREAS; ++Area)
	{
		const int32 N = Geo.AreaIndices[Area].Num();
		if (N > 0)
		{
			Indices.Append(Geo.AreaIndices[Area]);
			AreaCounts += FString::Printf(TEXT("%s\"%d\": %d"),
				AreaCounts.IsEmpty() ? TEXT("") : TEXT(", "), Area, N / 3);
		}
	}
	if (Verts.Num() == 0 || Indices.Num() < 3)
	{
		return Fail(FString::Printf(
			TEXT("navmesh present but empty (%d verts, %d walkable indices across %d area "
			     "slots) - the bounds volume may not cover any walkable surface"),
			Verts.Num(), Indices.Num(), RECAST_MAX_AREAS - 1));
	}

	// float32[V][3] vertices then int32[T][3] triangles, matching UE5-Agent-Data's layout so
	// the same offline planner reads both.
	TArray<uint8> Blob;
	Blob.Reserve(Verts.Num() * 12 + Indices.Num() * 4);
	FBox Bounds(ForceInit);
	for (const FVector& V : Verts)
	{
		Bounds += V;
		const float XYZ[3] = { (float)V.X, (float)V.Y, (float)V.Z };
		Blob.Append(reinterpret_cast<const uint8*>(XYZ), sizeof(XYZ));
	}
	Blob.Append(reinterpret_cast<const uint8*>(Indices.GetData()), Indices.Num() * sizeof(int32));

	IFileManager::Get().MakeDirectory(*FPaths::GetPath(OutBinPath), true);
	if (!FFileHelper::SaveArrayToFile(Blob, *OutBinPath))
	{
		return Fail(TEXT("could not write the navmesh blob"));
	}

	const FString Json = FString::Printf(
		TEXT("{\n")
		TEXT("  \"vertex_count\": %d,\n  \"triangle_count\": %d,\n")
		TEXT("  \"agent_radius_cm\": %.1f,\n  \"agent_height_cm\": %.1f,\n")
		TEXT("  \"bounds_min\": [%.1f, %.1f, %.1f],\n  \"bounds_max\": [%.1f, %.1f, %.1f],\n")
		TEXT("  \"layout\": \"float32[V][3] vertices, then int32[T][3] triangle indices\",\n")
		TEXT("  \"numpy\": \"v = np.frombuffer(b[:V*12], '<f4').reshape(-1,3); ")
		TEXT("t = np.frombuffer(b[V*12:], '<i4').reshape(-1,3)\",\n")
		TEXT("  \"units\": \"centimetres, Unreal left-handed Z-up\"\n}\n"),
		Verts.Num(), Indices.Num() / 3, Nav->AgentRadius, Nav->AgentHeight,
		Bounds.Min.X, Bounds.Min.Y, Bounds.Min.Z, Bounds.Max.X, Bounds.Max.Y, Bounds.Max.Z);
	FFileHelper::SaveStringToFile(Json, *OutJsonPath);

	return FString::Printf(
		TEXT("{\"ok\": true, \"vertex_count\": %d, \"triangle_count\": %d, ")
		TEXT("\"agent_radius_cm\": %.1f, \"agent_height_cm\": %.1f, ")
		TEXT("\"bounds_min\": [%.1f, %.1f, %.1f], \"bounds_max\": [%.1f, %.1f, %.1f], ")
		TEXT("\"bin\": \"%s\", \"json\": \"%s\", \"triangles_per_area\": {%s}}"),
		Verts.Num(), Indices.Num() / 3, Nav->AgentRadius, Nav->AgentHeight,
		Bounds.Min.X, Bounds.Min.Y, Bounds.Min.Z, Bounds.Max.X, Bounds.Max.Y, Bounds.Max.Z,
		*Esc(OutBinPath), *Esc(OutJsonPath), *AreaCounts);
}


// --------------------------------------------------------------------------- in-engine capture
namespace
{
	// One capture at a time, deliberately. Two actors writing the same episode directory would
	// interleave frames and the failure would look like a corrupt trajectory rather than a
	// scheduling mistake.
	TWeakObjectPtr<ASimWorldCaptureActor> GActive;
}

FString USimWorldCapture::StartCapture(UObject* WorldContextObject,
	const FString& FrozenJsonPath, const FString& OutDir, int32 Width, int32 Height,
	float FOVDegrees, float DepthMaxRangeM, bool bDepthFloat16, int32 JpegQuality, bool bWriteRgb,
	float ExposureBiasEV, bool bManualExposure, float AutoExposureSpeed,
	float AutoExposureMinBrightness, float AutoExposureMaxBrightness, float LocalExposureShadow,
	float LocalExposureHighlight, float LocalExposureDetail, int32 HistoryMode, int32 SuperSample)
{
	UWorld* World = GEngine ? GEngine->GetWorldFromContextObject(
		WorldContextObject, EGetWorldErrorMode::ReturnNull) : nullptr;
	if (!World)
	{
		return Fail(TEXT("no world"));
	}
	if (GActive.IsValid() && !GActive->IsFinished())
	{
		return Fail(TEXT("a capture is already running; poll GetCaptureStatus until it finishes"));
	}
	if (GActive.IsValid())
	{
		GActive->Destroy();
		GActive = nullptr;
	}

	ASimWorldCaptureActor* Actor = World->SpawnActor<ASimWorldCaptureActor>(
		ASimWorldCaptureActor::StaticClass(), FVector::ZeroVector, FRotator::ZeroRotator);
	if (!Actor)
	{
		return Fail(TEXT("could not spawn ASimWorldCaptureActor"));
	}
	const FString Err = Actor->Arm(FrozenJsonPath, OutDir, Width, Height, FOVDegrees,
		DepthMaxRangeM, bDepthFloat16, JpegQuality, bWriteRgb, ExposureBiasEV, bManualExposure,
		AutoExposureSpeed, AutoExposureMinBrightness, AutoExposureMaxBrightness,
		LocalExposureShadow, LocalExposureHighlight, LocalExposureDetail, HistoryMode, SuperSample);
	if (!Err.IsEmpty())
	{
		Actor->Destroy();
		return Fail(Err);
	}
	GActive = Actor;
	return Actor->StatusJson();
}

FString USimWorldCapture::StartWalkerCapture(UObject* WorldContextObject,
	const FString& FrozenJsonPath, const FString& OutDir, int32 Width, int32 Height,
	float FOVDegrees, const FString& MeshPathsCsv, const FString& WalkAnimPath,
	const FString& FacePath, const FString& WalkerBlueprintPath, float EyeHeightCm,
	float AnimForwardSpeedCmS, float CamBehindCm,
	float CamAboveCm, float CamLookAtZCm, float CamSideCm, float CamPitchDeg,
	bool bCameraFollowsView, float DepthMaxRangeM, bool bDepthFloat16, int32 JpegQuality)
{
	UWorld* World = GEngine ? GEngine->GetWorldFromContextObject(
		WorldContextObject, EGetWorldErrorMode::ReturnNull) : nullptr;
	if (!World)
	{
		return Fail(TEXT("no world"));
	}
	if (GActive.IsValid() && !GActive->IsFinished())
	{
		return Fail(TEXT("a capture is already running; poll GetCaptureStatus until it finishes"));
	}
	if (GActive.IsValid())
	{
		GActive->Destroy();
		GActive = nullptr;
	}

	// Sweep up any walker left behind by an earlier run, including one from before the capture
	// actor took ownership of its lifetime. Named, not tagged, because the name is what survives.
	for (TActorIterator<AActor> It(World); It; ++It)
	{
		if (It->GetName().Contains(TEXT("SimWorldWalker")))
		{
			It->Destroy();
		}
	}

	ASimWorldCaptureActor* Actor = World->SpawnActor<ASimWorldCaptureActor>(
		ASimWorldCaptureActor::StaticClass(), FVector::ZeroVector, FRotator::ZeroRotator);
	if (!Actor)
	{
		return Fail(TEXT("could not spawn ASimWorldCaptureActor"));
	}
	// Arm first: the walker needs the poses parsed before it can be placed on frame 0.
	FString Err = Actor->Arm(FrozenJsonPath, OutDir, Width, Height, FOVDegrees, DepthMaxRangeM,
		bDepthFloat16, JpegQuality, true);
	if (Err.IsEmpty())
	{
		Err = Actor->AttachWalker(MeshPathsCsv, WalkAnimPath, FacePath, WalkerBlueprintPath,
			EyeHeightCm,
			AnimForwardSpeedCmS, CamBehindCm, CamAboveCm, CamLookAtZCm, CamSideCm, CamPitchDeg,
			bCameraFollowsView);
	}
	if (!Err.IsEmpty())
	{
		Actor->Destroy();
		return Fail(Err);
	}
	GActive = Actor;
	return Actor->StatusJson();
}

FString USimWorldCapture::GetCaptureStatus(UObject* WorldContextObject)
{
	if (!GActive.IsValid())
	{
		return Fail(TEXT("no capture has been started in this editor session"));
	}
	return GActive->StatusJson();
}
