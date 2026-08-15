"""Real Cosmos-Transfer2.5 inference runner.

Shared by the sim2real augment stage, the Cosmos synthetic fan-out workflow, and
the ``npa workbench cosmos2 transfer`` CLI so they run the actual world-transfer
model (video-to-video) instead of writing descriptor stubs.

The transfer runtime lives in the ``npa-cosmos2-transfer`` image at
``/opt/cosmos/cosmos-transfer2.5`` (Python 3.10 + torch cu128 + flash-attn in its
own ``.venv``). This module shells out to that venv's ``examples/inference.py`` so
it stays import-safe on the default interpreter (no torch/cuda import here).

Callers that run outside the transfer image (unit tests, CPU hosts) should guard
on :func:`cosmos_transfer_available` and fall back to their descriptor path.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_REPO = "/opt/cosmos/cosmos-transfer2.5"
# No upstream media is bundled in the redistributable image. Callers must supply
# either an input clip (the preferred path) or an explicit operator-owned spec.
DEFAULT_SPEC = ""

# Control modalities Cosmos Transfer 2.5 computes ON-THE-FLY from the input
# ``video_path`` (Canny edge / bilateral blur), so conditioning on an arbitrary
# input clip needs NO precomputed control asset. depth/seg require a precomputed
# control file, so they are not used for self-contained input-only conditioning.
INPUT_AUTO_CONTROLS = ("edge", "vis")
DEFAULT_INPUT_CONTROL = "edge"
DISABLE_CONTENT_GUARDRAILS_ENV = "NPA_COSMOS_DISABLE_CONTENT_GUARDRAILS"
# Live job 339 reported SUCCEEDED while the spec promised ``manifest.json`` and
# the then-reference-only tool wrote ``index.json`` with a different schema.
# Keep these two artifact contracts named and distinct: the real publisher now
# writes the canonical transfer manifest, while reference augmentation retains
# its frame index. ``test_spec_declared_outputs`` binds workflow declarations to
# the appropriate helper so this cannot regress into another false success.
TRANSFER_MANIFEST_FILENAME = "manifest.json"
TRANSFER_MANIFEST_SCHEMA = "npa.cosmos2.transfer.v1"
TRANSFER_MANIFEST_MODE = "cosmos_transfer2.5_gpu"
TRANSFER_MANIFEST_STATUS = "executed"
AUGMENTED_FRAMES_INDEX = "index.json"
AUGMENTED_FRAMES_SCHEMA = "npa.sim2real.augmented_frames.v1"
REFERENCE_AUGMENT_MODE = "reference_augment"
REFERENCE_AUGMENT_STATUS = "executed_reference"
# Neutral photoreal prompt used when the caller conditions on an input clip but
# supplies no appearance prompt of its own.
_DEFAULT_INPUT_PROMPT = (
    "photorealistic, natural lighting, high detail, sharp focus, realistic textures"
)


class FrameExtractionError(RuntimeError):
    """Raised when the frame-extraction subprocess cannot decode a video."""


class ProtectedChromaError(RuntimeError):
    """Raised when configured protected-region color preservation cannot complete."""


def _parse_protected_regions(value: str) -> list[tuple[float, float, float, float]]:
    """Parse normalized protected rectangles for source-chroma restoration."""

    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ProtectedChromaError("protected regions must be valid JSON") from exc
    if not isinstance(raw, list) or not raw:
        raise ProtectedChromaError("source-chroma mode requires protected regions")
    regions: list[tuple[float, float, float, float]] = []
    for index, item in enumerate(raw):
        bounds: Any = item.get("bounds") if isinstance(item, dict) else item
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            raise ProtectedChromaError(
                f"protected region {index} must have four normalized bounds"
            )
        try:
            x0, y0, x1, y1 = (float(part) for part in bounds)
        except (TypeError, ValueError) as exc:
            raise ProtectedChromaError(
                f"protected region {index} has non-numeric bounds"
            ) from exc
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            raise ProtectedChromaError(
                f"protected region {index} bounds must satisfy "
                "0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1"
            )
        regions.append((x0, y0, x1, y1))
    return regions


def preserve_source_chroma(
    transfer: dict[str, Any],
    *,
    source_video: str,
    regions_json: str = "",
    masks_dir: str = "",
    segmentation: dict[str, Any] | None = None,
    feather_pixels: int = 12,
    luma_max_delta: int = 32,
) -> dict[str, Any]:
    """Restore source chroma in protected regions while retaining generated light.

    Cosmos still generates every frame. This optional deterministic post-process
    restores source Cb/Cr per pixel inside feathered normalized rectangles or
    frame-aligned SAM2 masks;
    generated luma is retained within a bounded per-pixel distance from source,
    so mild illumination/exposure augmentation remains visible while extreme
    darkening or brightening fails to alter protected identity colors. Exact
    frame-count and geometry alignment are required to avoid color ghosts across
    moving boundaries.
    Frame-count or decode mismatches fail closed instead of publishing partially
    corrected output.
    """

    if bool(regions_json) == bool(masks_dir):
        raise ProtectedChromaError(
            "protected chroma requires exactly one of regions_json or masks_dir"
        )
    regions = _parse_protected_regions(regions_json) if regions_json else []
    mask_root = Path(masks_dir) if masks_dir else None
    if mask_root is not None and not mask_root.is_dir():
        raise ProtectedChromaError("protected SAM2 mask directory is missing")
    if feather_pixels < 1:
        raise ProtectedChromaError("protected chroma feather must be positive")
    if not 0 <= luma_max_delta <= 255:
        raise ProtectedChromaError("protected luma max delta must be within 0..255")
    source = Path(source_video)
    augmented = Path(str(transfer.get("video_path") or ""))
    if not source.is_file() or not augmented.is_file():
        raise ProtectedChromaError(
            "protected chroma needs readable source and augmented videos"
        )
    output = augmented.with_name(f"{augmented.stem}-source-chroma.mp4")
    script = r'''
import av, json, numpy as np, sys
from pathlib import Path
source_path, augmented_path, frames_dir_text, regions_text, masks_dir_text, feather_text, luma_delta_text = sys.argv[1:]
frames_dir = Path(frames_dir_text)
regions = json.loads(regions_text) if regions_text else []
regions = [r.get("bounds") if isinstance(r, dict) else r for r in regions]
masks_dir = Path(masks_dir_text) if masks_dir_text else None
feather = int(feather_text)
luma_delta = int(luma_delta_text)
src_container = av.open(source_path)
aug_container = av.open(augmented_path)
aug_stream = aug_container.streams.video[0]
rate = aug_stream.average_rate or aug_stream.base_rate or 30
width, height = int(aug_stream.width), int(aug_stream.height)
masks = []
for bounds in regions:
    x0 = max(0, min(width - 1, int(round(float(bounds[0]) * width))))
    y0 = max(0, min(height - 1, int(round(float(bounds[1]) * height))))
    x1 = max(x0 + 1, min(width, int(round(float(bounds[2]) * width))))
    y1 = max(y0 + 1, min(height, int(round(float(bounds[3]) * height))))
    yy, xx = np.ogrid[y0:y1, x0:x1]
    distance = np.minimum(
        np.minimum(xx - x0, x1 - 1 - xx),
        np.minimum(yy - y0, y1 - 1 - yy),
    ).astype(np.float32)
    alpha = np.clip((distance + 1.0) / float(feather), 0.0, 1.0)
    mask = np.zeros((height, width), dtype=np.float32)
    mask[y0:y1, x0:x1] = alpha
    masks.append(mask)
rect_alpha = np.maximum.reduce(masks) if masks else None
src_frames = iter(src_container.decode(video=0))
aug_frames = iter(aug_container.decode(video=0))
count = 0
for aug_frame in aug_frames:
    try:
        src_frame = next(src_frames)
    except StopIteration as exc:
        raise RuntimeError("source has fewer frames than augmented clip") from exc
    src = src_frame.reformat(width=width, height=height, format="rgb24").to_ndarray()
    aug = aug_frame.reformat(width=width, height=height, format="rgb24").to_ndarray()
    srcf, augf = src.astype(np.float32), aug.astype(np.float32)
    src_cb = 128.0 - 0.168736 * srcf[..., 0] - 0.331264 * srcf[..., 1] + 0.5 * srcf[..., 2]
    src_cr = 128.0 + 0.5 * srcf[..., 0] - 0.418688 * srcf[..., 1] - 0.081312 * srcf[..., 2]
    src_y = 0.299 * srcf[..., 0] + 0.587 * srcf[..., 1] + 0.114 * srcf[..., 2]
    y = 0.299 * augf[..., 0] + 0.587 * augf[..., 1] + 0.114 * augf[..., 2]
    aug_cb = 128.0 - 0.168736 * augf[..., 0] - 0.331264 * augf[..., 1] + 0.5 * augf[..., 2]
    aug_cr = 128.0 + 0.5 * augf[..., 0] - 0.418688 * augf[..., 1] - 0.081312 * augf[..., 2]
    if masks_dir is not None:
        from PIL import Image, ImageFilter
        mask_path = masks_dir / f"mask-{count:06d}.png"
        if not mask_path.is_file():
            raise RuntimeError(f"missing frame-aligned protected mask {mask_path.name}")
        with Image.open(mask_path) as opened_mask:
            mask_image = opened_mask.convert("L").resize((width, height), Image.Resampling.NEAREST)
        binary_mask = np.asarray(mask_image, dtype=np.float32) / 255.0
        if feather > 1:
            mask_image = mask_image.filter(ImageFilter.GaussianBlur(radius=max(0.5, feather / 2.0)))
            # Feather inward only. Multiplying by the original binary mask keeps
            # source chroma from bleeding into unprotected augmentation pixels.
            alpha = (np.asarray(mask_image, dtype=np.float32) / 255.0) * binary_mask
        else:
            alpha = binary_mask
    else:
        if rect_alpha is None:
            raise RuntimeError("protected region mask is missing")
        alpha = rect_alpha
    bounded_y = np.clip(y, src_y - float(luma_delta), src_y + float(luma_delta))
    y = y * (1.0 - alpha) + bounded_y * alpha
    cb = aug_cb * (1.0 - alpha) + src_cb * alpha
    cr = aug_cr * (1.0 - alpha) + src_cr * alpha
    rgb = np.stack((
        y + 1.402 * (cr - 128.0),
        y - 0.344136 * (cb - 128.0) - 0.714136 * (cr - 128.0),
        y + 1.772 * (cb - 128.0),
    ), axis=-1)
    frame = av.VideoFrame.from_ndarray(np.clip(rgb, 0, 255).astype(np.uint8), format="rgb24")
    frame.to_image().save(frames_dir / f"frame-{count:06d}.png")
    count += 1
try:
    next(src_frames)
except StopIteration:
    pass
else:
    raise RuntimeError("source has more frames than augmented clip")
aug_container.close(); src_container.close()
if count == 0:
    raise RuntimeError("no frames decoded")
if masks_dir is not None:
    mask_files = sorted(masks_dir.glob("mask-*.png"))
    if len(mask_files) != count:
        raise RuntimeError("protected SAM2 mask count differs from video frame count")
print(json.dumps({"frames": count, "fps": float(rate)}))
'''
    try:
        with tempfile.TemporaryDirectory(
            prefix="npa-protected-chroma-", dir=str(augmented.parent)
        ) as frames_dir:
            completed = subprocess.run(
                [
                    str(_venv_python(cosmos_transfer_repo())),
                    "-c",
                    script,
                    str(source),
                    str(augmented),
                    frames_dir,
                    regions_json,
                    str(mask_root or ""),
                    str(feather_pixels),
                    str(luma_max_delta),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            try:
                decoded = json.loads(str(completed.stdout).strip().splitlines()[-1])
                frame_count = int(decoded["frames"])
                fps = float(decoded["fps"])
            except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ProtectedChromaError(
                    "protected source-chroma decoder returned invalid metadata"
                ) from exc
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-framerate",
                    str(fps),
                    "-i",
                    str(Path(frames_dir) / "frame-%06d.png"),
                    "-an",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-crf",
                    "18",
                    "-movflags",
                    "+faststart",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
    except ProtectedChromaError:
        raise
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = str(getattr(exc, "stderr", "") or exc).strip()
        raise ProtectedChromaError(
            f"protected source-chroma restoration failed: {detail}"[:300]
        ) from exc
    if not output.is_file() or output.stat().st_size <= 0:
        raise ProtectedChromaError("protected source-chroma restoration wrote no video")
    result = dict(transfer)
    result["video_path"] = str(output)
    result["video_bytes"] = output.stat().st_size
    result["protected_chroma"] = {
        "mode": "source-chroma",
        "method": (
            "sam2-mask-feathered-per-pixel-source-chroma"
            if mask_root is not None
            else "feathered-per-pixel-source-chroma"
        ),
        "region_count": len(regions),
        "feather_pixels": feather_pixels,
        "luma_max_delta": luma_max_delta,
        "frame_count": frame_count,
    }
    if mask_root is not None:
        result["protected_chroma"]["segmentation"] = segmentation or {
            "engine": "meta-sam2-upstream"
        }
    return result


def _spec_for_input_video(
    repo: Path,
    *,
    input_video: str,
    prompt: str,
    control: str,
    control_weight: float,
    guidance: float,
    name: str,
) -> tuple[str, str]:
    """Write a Cosmos Transfer 2.5 controlnet spec that CONDITIONS ON ``input_video``.

    ``video_path`` is the caller's real input clip; the ``edge``/``vis`` control is
    computed on-the-fly from it (no precomputed control asset), so the output
    preserves the input's structure/motion while ``prompt`` drives a new
    appearance -- i.e. a genuine augmentation of the caller's footage. Returns
    ``(spec_path_relative_to_repo, control_modality)``.
    """
    import json as _json

    modality = str(control or DEFAULT_INPUT_CONTROL).strip().lower()
    if modality not in INPUT_AUTO_CONTROLS:
        modality = DEFAULT_INPUT_CONTROL
    spec = {
        "name": str(name or "npa_input"),
        "prompt": str(prompt or "").strip() or _DEFAULT_INPUT_PROMPT,
        # Absolute path so it resolves regardless of where the spec file lives.
        "video_path": str(Path(input_video).resolve()),
        "guidance": guidance,
        modality: {"control_weight": float(control_weight)},
    }
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(name or "input"))
    spec_path = repo / f"_npa_input_spec_{safe}.json"
    spec_path.write_text(_json.dumps(spec, indent=2), encoding="utf-8")
    return str(spec_path.relative_to(repo)), modality


def cosmos_transfer_repo() -> Path:
    return Path(os.environ.get("COSMOS_TRANSFER_REPO", DEFAULT_REPO))


def _venv_python(repo: Path) -> Path:
    return repo / ".venv" / "bin" / "python"


def _venv_has_torch(py: Path) -> bool:
    # Probe defensively: a mirrored/hardened transfer image can make the venv
    # python unreadable (stat -> PermissionError) or non-executable. Treat any
    # OSError as "runtime unavailable" so callers fall back to the descriptor
    # path instead of crashing the augment stage.
    try:
        if not py.exists():
            return False
    except OSError:
        return False
    try:
        proc = subprocess.run(
            [str(py), "-c", "import torch, flash_attn"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return proc.returncode == 0


def cosmos_transfer_available() -> bool:
    """True when the real Cosmos-Transfer2.5 runtime is present and runnable.

    The redistributable image bakes the locked inference venv. Runtime dependency
    self-healing would make the executed dependency set differ from the audited
    image, so a missing venv is unavailable rather than a cue to download packages.
    """

    repo = cosmos_transfer_repo()
    if not (repo / "examples" / "inference.py").is_file():
        return False
    return _venv_has_torch(_venv_python(repo))


def ensure_env(repo: Path) -> Path:
    """Return the audited inference venv; never mutate or download at run time."""

    py = _venv_python(repo)
    if _venv_has_torch(py):
        return py
    raise RuntimeError(
        "cosmos-transfer2.5 audited inference venv is missing or unusable; "
        "rebuild the pinned npa-cosmos2-transfer image"
    )


def _require_runtime_hf_token() -> None:
    """Refuse gated-model inference before any anonymous/partial download starts."""

    if not os.environ.get("HF_TOKEN", "").strip():
        raise RuntimeError(
            "HF_TOKEN is required at run time for gated Cosmos Transfer weights; "
            "no model download was attempted"
        )


def _spec_with_prompt(repo: Path, spec: str, prompt: str, *, tag: str = "") -> str:
    """Write a copy of ``spec`` with its text prompt overridden; return its path.

    Cosmos controlnet specs carry the text prompt that steers appearance. Patching
    it lets the sampled appearance combo actually condition the diffusion (same
    control video / motion, new look) instead of being a decorative label. The
    copy sits next to the original so relative control-asset paths still resolve.
    ``tag`` makes the patched filename unique per variant so concurrent multiply
    fan-out (one inference per GPU) never clobbers a sibling's spec.
    Best-effort: on any failure we fall back to the original spec.
    """
    import json as _json

    try:
        spec_path = repo / spec
        data = _json.loads(spec_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return spec
        data["prompt"] = prompt
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(tag or ""))
        prefix = f"_npa_prompted_{safe}_" if safe else "_npa_prompted_"
        patched = spec_path.with_name(prefix + spec_path.name)
        patched.write_text(_json.dumps(data, indent=2), encoding="utf-8")
        return str(patched.relative_to(repo))
    except Exception:  # noqa: BLE001 - prompt override is best-effort
        return spec


def run_cosmos_transfer(
    *,
    run_id: str = "",
    spec: str | None = None,
    prompt: str | None = None,
    out_subdir: str | None = None,
    hf_home: str | None = None,
    input_video: str | None = None,
    control: str = DEFAULT_INPUT_CONTROL,
    control_weight: float = 1.0,
    guidance: float = 3.0,
    cuda_visible_devices: str | None = None,
    variant_tag: str = "",
    disable_content_guardrails: bool | None = None,
) -> dict[str, Any]:
    """Run a real Cosmos-Transfer2.5 inference; return the generated video + metadata.

    ``spec`` is a controlnet spec path relative to the transfer repo (or the
    ``COSMOS_TRANSFER_SPEC`` environment override). No upstream fixture is baked.
    ``prompt`` (or ``COSMOS_TRANSFER_PROMPT``), when set, overrides the spec's text
    prompt so the sampled appearance actually conditions the augmentation.

    When ``input_video`` is provided the transfer is CONDITIONED ON THAT CLIP: a
    controlnet spec is built with ``video_path`` = the input and an ``edge``/``vis``
    control computed on-the-fly, so the output is a real augmentation of the
    caller's footage (new appearance from ``prompt``, same structure/motion).
    When ``input_video`` is absent, the caller must provide an operator-owned spec.
    """

    repo = cosmos_transfer_repo()
    _require_runtime_hf_token()
    py = ensure_env(repo)
    tag = str(variant_tag or run_id or "input")
    conditioned_control = ""
    if input_video:
        spec, conditioned_control = _spec_for_input_video(
            repo,
            input_video=input_video,
            prompt=prompt or os.environ.get("COSMOS_TRANSFER_PROMPT", ""),
            control=control,
            control_weight=control_weight,
            guidance=guidance,
            name=tag,
        )
    else:
        spec = spec or os.environ.get("COSMOS_TRANSFER_SPEC", DEFAULT_SPEC)
        if not spec:
            raise ValueError(
                "Cosmos Transfer inference requires input_video or an explicit "
                "COSMOS_TRANSFER_SPEC; no upstream media is bundled"
            )
        prompt = prompt or os.environ.get("COSMOS_TRANSFER_PROMPT", "")
        if prompt:
            spec = _spec_with_prompt(repo, spec, prompt, tag=tag)
    out = out_subdir or f"outputs/{run_id or 'transfer'}"
    out_abs = repo / out
    if out_abs.exists():
        shutil.rmtree(out_abs)

    env = dict(os.environ)
    env["HF_HOME"] = hf_home or os.environ.get("HF_HOME", "/opt/cosmos-data/hf_cache")
    env.setdefault("TOKENIZERS_PARALLELISM", "false")
    if cuda_visible_devices is not None and str(cuda_visible_devices).strip() != "":
        env["CUDA_VISIBLE_DEVICES"] = str(cuda_visible_devices).strip()
    # Only the specs WE synthesized this call are ephemeral; never delete a
    # caller-supplied spec. Per-variant tags keep siblings
    # from clobbering each other, so removing exactly our file is fan-out safe.
    # Capture its content first so callers can still inspect the effective spec
    # after the file is gone (nothing depends on the ephemeral file persisting).
    temp_spec = repo / spec if Path(spec).name.startswith(("_npa_input_spec_", "_npa_prompted_")) else None
    spec_json: dict[str, Any] | None = None
    if temp_spec is not None:
        try:
            import json as _json

            spec_json = _json.loads(temp_spec.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            spec_json = None
    if disable_content_guardrails is None:
        disable_content_guardrails = os.environ.get(
            DISABLE_CONTENT_GUARDRAILS_ENV, ""
        ).strip().lower() in {"1", "true", "yes", "on"}
    argv = [str(py), "examples/inference.py", "-i", spec, "-o", out]
    if disable_content_guardrails:
        # Upstream exposes this explicit setup option for domains whose valid
        # generated pixels are outside the generic video guardrail's calibration
        # set. Keep the NPA default fail-closed; operators must opt out per run.
        argv.append("--disable-guardrails")
    try:
        # Upstream progress output includes the effective prompt and local input
        # path. Keep it in an unnamed, process-local file that is destroyed at
        # completion; the retained task log reports only aggregate NPA evidence.
        with tempfile.TemporaryFile() as vendor_log:
            subprocess.run(
                argv,
                cwd=repo,
                env=env,
                check=True,
                stdout=vendor_log,
                stderr=subprocess.STDOUT,
            )
    except (OSError, subprocess.CalledProcessError):
        raise RuntimeError(
            "Cosmos Transfer inference failed; inspect GPU/model access and retry"
        ) from None
    finally:
        if temp_spec is not None:
            try:
                temp_spec.unlink()
            except OSError:
                pass

    videos = [
        f
        for f in glob.glob(str(out_abs / "**" / "*.mp4"), recursive=True)
        if "control" not in Path(f).name
    ]
    # Upstream already ran its generated-video guardrail before writing this
    # file. Do not reuse the container golden-eval's 100 KiB heuristic here:
    # a short valid transfer can produce a ~9 KiB video (live job 371). S3
    # publication below still fails closed unless PyAV can decode at least one
    # exact frame, which is the artifact contract consumers need.
    produced = sorted(
        (f for f in videos if os.path.getsize(f) > 0),
        key=os.path.getsize,
        reverse=True,
    )
    if not produced:
        raise RuntimeError(f"cosmos-transfer2.5 produced no output video in {out_abs}")
    control_videos = [
        f for f in glob.glob(str(out_abs / "**" / "*.mp4"), recursive=True)
        if "control" in Path(f).name
    ]
    return {
        "video_path": produced[0],
        "video_bytes": os.path.getsize(produced[0]),
        "control_path": control_videos[0] if control_videos else "",
        "out_dir": str(out_abs),
        "spec": spec,
        "spec_json": spec_json,
        "repo": str(repo),
        "input_conditioned": bool(input_video),
        "input_video": str(input_video or ""),
        "control": conditioned_control,
        "content_guardrails_enabled": not disable_content_guardrails,
    }


def extract_frames(video_path: str, dest_dir: Path, *, max_frames: int = 8) -> list[Path]:
    """Extract up to ``max_frames`` evenly-spaced PNG frames from ``video_path``.

    Runs in the transfer venv (which ships PyAV). A successful decode with no
    video frames returns ``[]``; subprocess and PyAV failures retain their stderr
    and original exception as :class:`FrameExtractionError`.
    """

    repo = cosmos_transfer_repo()
    py = _venv_python(repo)
    dest_dir.mkdir(parents=True, exist_ok=True)
    script = (
        "import av, sys\n"
        "from pathlib import Path\n"
        "vp, dest, n = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])\n"
        "with av.open(vp) as c:\n"
        "    frames = [f for f in c.decode(video=0)]\n"
        "step = max(1, len(frames) // n) if frames else 1\n"
        "sel = frames[::step][:n]\n"
        "for i, fr in enumerate(sel):\n"
        "    fr.to_image().save(str(dest / f'frame-{i:05d}.png'))\n"
        "print(len(sel))\n"
    )
    try:
        subprocess.run(
            [str(py), "-c", script, video_path, str(dest_dir), str(max_frames)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = str(exc.stderr or exc.stdout or exc).strip()
        raise FrameExtractionError(
            f"frame extraction failed for {video_path!r} with exit code "
            f"{exc.returncode}: {detail}"
        ) from exc
    except OSError as exc:
        raise FrameExtractionError(
            f"could not start frame extraction for {video_path!r}: {exc}"
        ) from exc
    return sorted(dest_dir.glob("frame-*.png"))


def publish_transfer_clip(
    transfer: dict[str, Any],
    output_uri: str,
    *,
    run_id: str = "",
    clip_name: str = "",
    variables: dict[str, Any] | None = None,
    max_frames: int = 8,
    frames_output_uri: str = "",
    require_frames: bool = False,
    storage_client: Any = None,
) -> dict[str, Any]:
    """Publish ONE real Cosmos-Transfer2.5 result as a per-clip dir under
    ``output_uri`` (the ``cosmos_augmented/`` prefix), returning the clip's
    descriptor (no run-level manifest is written here).

    Writes:

        <clip>/augmented_video.mp4
        <clip>/frame-00000.png ... (or ``frames_output_uri/frame-*.png``)
        <clip>/metadata.json      (variables + mode, for the Rerun label)

    This is the unit of "multiply": the caller runs one inference per sampled
    appearance combo and publishes each as its own clip, then calls
    :func:`write_run_manifest` once to emit the run-level ``manifest.json``.
    """

    if not output_uri.startswith("s3://"):
        raise ValueError(f"output_uri must be an s3:// prefix, got: {output_uri!r}")
    from npa.clients.storage import StorageClient

    client = storage_client or StorageClient.from_environment()
    base = output_uri if output_uri.endswith("/") else output_uri + "/"
    clip = clip_name or (f"aug-{run_id}" if run_id else "aug0")
    clip_base = f"{base}{clip}/"
    frames_base = (
        frames_output_uri.rstrip("/") + "/" if frames_output_uri else clip_base
    )
    video_uri = f"{clip_base}augmented_video.mp4"

    import json as _json
    import tempfile as _tempfile

    # This publish path only runs after the REAL Cosmos Transfer 2.5 model
    # executed on GPU, so record the GPU mode (kept in sync with the provenance
    # classifier in data_factory_provenance.py). When the transfer was
    # conditioned on the caller's input clip, record that provenance so the run
    # view can show the augmentation is genuinely derived from real input.
    input_conditioned = bool(transfer.get("input_conditioned"))
    conditioned_input = Path(str(transfer.get("input_video") or "")).name
    conditioned_control = str(transfer.get("control") or "")
    content_guardrails_enabled = bool(
        transfer.get("content_guardrails_enabled", True)
    )
    protected_chroma = transfer.get("protected_chroma") or {"mode": "off"}
    refinement = transfer.get("refinement") or {}
    effective_control_weight = transfer.get("effective_control_weight")
    effective_guidance = transfer.get("effective_guidance")
    conditioning_clip_uri = str(transfer.get("conditioning_clip_uri") or "")

    frame_index: list[dict[str, str]] = []
    with _tempfile.TemporaryDirectory(prefix="npa-cosmos-pub-") as tmp:
        frames = extract_frames(transfer["video_path"], Path(tmp) / "frames", max_frames=max_frames)
        if require_frames and not frames:
            raise RuntimeError(
                "Cosmos Transfer completed but no frames could be extracted from "
                f"{transfer['video_path']!r}; refusing to publish a manifest whose "
                "augmented_frames_uri has no frame-NNNNN.png objects."
            )
        # Validate the required frame contract before publishing any object. A
        # zero-frame decode must not leave a plausible video-only success behind.
        client.upload_file(transfer["video_path"], video_uri)
        for i, frame_path in enumerate(frames):
            key = f"frame-{i:05d}.png"
            client.upload_file(str(frame_path), f"{frames_base}{key}")
            frame_index.append({"frame_id": f"frame-{i:05d}", "uri": f"{frames_base}{key}"})

        clip_meta = {
            "schema": TRANSFER_MANIFEST_SCHEMA,
            "mode": TRANSFER_MANIFEST_MODE,
            "clip": clip,
            "variables": variables or {},
            "prompt": str((variables or {}).get("prompt") or ""),
            "control_spec": transfer.get("spec", ""),
            "input_conditioned": input_conditioned,
            "conditioned_input": conditioned_input,
            "conditioning_clip_uri": conditioning_clip_uri,
            "control": conditioned_control,
            "content_guardrails_enabled": content_guardrails_enabled,
            "protected_chroma": protected_chroma,
            "refinement": refinement,
            "effective_control_weight": effective_control_weight,
            "effective_guidance": effective_guidance,
        }
        cm = Path(tmp) / "metadata.json"
        cm.write_text(_json.dumps(clip_meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        client.upload_file(str(cm), f"{clip_base}metadata.json")

    return {
        "clip": clip,
        "clip_base": clip_base,
        "augmented_video_uri": video_uri,
        "frame_count": len(frame_index),
        "frames": frame_index,
        "frames_uri": frames_base,
        "control_spec": transfer.get("spec", ""),
        "video_bytes": int(transfer.get("video_bytes", 0) or 0),
        "input_conditioned": input_conditioned,
        "conditioned_input": conditioned_input,
        "conditioning_clip_uri": conditioning_clip_uri,
        "control": conditioned_control,
        "content_guardrails_enabled": content_guardrails_enabled,
        "protected_chroma": protected_chroma,
        "refinement": refinement,
        "effective_control_weight": effective_control_weight,
        "effective_guidance": effective_guidance,
        "variables": variables or {},
    }


def write_run_manifest(
    clips: list[dict[str, Any]],
    output_uri: str,
    *,
    run_id: str = "",
    storage_client: Any = None,
    variant_parallelism: int = 1,
) -> dict[str, Any]:
    """Write the run-level ``cosmos_augmented/manifest.json`` listing every clip
    produced by the (possibly multi-variant) augment stage; return the manifest.

    ``clips`` are the descriptors returned by :func:`publish_transfer_clip`.
    ``variant_parallelism`` records how many GPUs the fan-out ran across (1 ==
    sequential) so provenance can surface the multi-GPU amplification.
    """

    if not output_uri.startswith("s3://"):
        raise ValueError(f"output_uri must be an s3:// prefix, got: {output_uri!r}")
    from npa.clients.storage import StorageClient

    import json as _json
    import tempfile as _tempfile

    client = storage_client or StorageClient.from_environment()
    first = clips[0] if clips else {}
    frames = [f for c in clips for f in c.get("frames", [])]
    manifest = {
        "schema": TRANSFER_MANIFEST_SCHEMA,
        "mode": TRANSFER_MANIFEST_MODE,
        "status": TRANSFER_MANIFEST_STATUS,
        "run_id": run_id,
        "clips": [c.get("clip", "") for c in clips],
        "variant_count": len(clips),
        # "multiply": one Cosmos Transfer 2.5 inference per sampled appearance
        # combo. >1 clips means the run genuinely amplified across scenarios.
        "multiply_mode": "multi-variant" if len(clips) > 1 else "single-variant",
        "variant_parallelism": max(1, int(variant_parallelism or 1)),
        "augmented_video_uri": first.get("augmented_video_uri", ""),
        "augmented_videos": [c.get("augmented_video_uri", "") for c in clips],
        "frame_count": sum(int(c.get("frame_count", 0) or 0) for c in clips),
        "frames": frames,
        "augmented_frames_uri": first.get("frames_uri", ""),
        "control_spec": first.get("control_spec", ""),
        "video_bytes": sum(int(c.get("video_bytes", 0) or 0) for c in clips),
        "input_conditioned": bool(first.get("input_conditioned")),
        "conditioned_input": first.get("conditioned_input", ""),
        "conditioning_clip_uri": first.get("conditioning_clip_uri", ""),
        "control": first.get("control", ""),
        "content_guardrails_enabled": bool(
            first.get("content_guardrails_enabled", True)
        ),
        "protected_chroma": first.get("protected_chroma", {"mode": "off"}),
        "refinement": first.get("refinement", {}),
        "effective_control_weight": first.get("effective_control_weight"),
        "effective_guidance": first.get("effective_guidance"),
        "variants": [
            {
                "clip": c.get("clip", ""),
                "variables": c.get("variables", {}),
                "prompt": str((c.get("variables") or {}).get("prompt") or ""),
                "frame_count": int(c.get("frame_count", 0) or 0),
                "augmented_video_uri": c.get("augmented_video_uri", ""),
                "protected_chroma": c.get("protected_chroma", {"mode": "off"}),
                "refinement": c.get("refinement", {}),
                "effective_control_weight": c.get("effective_control_weight"),
                "effective_guidance": c.get("effective_guidance"),
            }
            for c in clips
        ],
    }
    with _tempfile.TemporaryDirectory(prefix="npa-cosmos-man-") as tmp:
        mp = Path(tmp) / TRANSFER_MANIFEST_FILENAME
        mp.write_text(_json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        client.upload_file(str(mp), transfer_manifest_uri_for(output_uri))
    return manifest


def publish_transfer_to_s3(
    transfer: dict[str, Any],
    output_uri: str,
    *,
    run_id: str = "",
    variables: dict[str, Any] | None = None,
    clip_name: str = "",
    max_frames: int = 8,
    frames_output_uri: str = "",
    require_frames: bool = False,
    storage_client: Any = None,
) -> dict[str, Any]:
    """Upload a single real Cosmos-Transfer2.5 result to S3 in the per-clip layout
    that ``data_factory_stages.curate`` and ``data_factory_viz.build_run_rrd``
    consume, plus the run-level manifest. Single-variant convenience wrapper
    around :func:`publish_transfer_clip` + :func:`write_run_manifest`; multi-variant
    callers publish each clip themselves and write one combined manifest.
    """

    clip = publish_transfer_clip(
        transfer,
        output_uri,
        run_id=run_id,
        clip_name=clip_name,
        variables=variables,
        max_frames=max_frames,
        frames_output_uri=frames_output_uri,
        require_frames=require_frames,
        storage_client=storage_client,
    )
    return write_run_manifest([clip], output_uri, run_id=run_id, storage_client=storage_client)


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".ppm", ".webp"}
_PERTURBATIONS = ("lighting", "contrast", "color", "blur")


def transfer_manifest_uri_for(output_uri: str) -> str:
    """Return the durable manifest URI written by a real transfer publish."""

    return output_uri.rstrip("/") + "/" + TRANSFER_MANIFEST_FILENAME


def augmented_frames_index_uri_for(output_uri: str) -> str:
    """Return the index URI written by reference augmentation."""

    return output_uri.rstrip("/") + "/" + AUGMENTED_FRAMES_INDEX


def _apply_perturbation(image: Any, perturbation: str, *, seed: int) -> Any:
    """Apply one deterministic, real image transform (a perturbation ControlNet
    would drive in the full model; here a genuine PIL transform, not a no-op)."""

    import random

    from PIL import ImageEnhance, ImageFilter

    rng = random.Random(seed)
    if perturbation == "lighting":
        return ImageEnhance.Brightness(image).enhance(rng.uniform(0.55, 1.6))
    if perturbation == "contrast":
        return ImageEnhance.Contrast(image).enhance(rng.uniform(0.6, 1.7))
    if perturbation == "color":
        return ImageEnhance.Color(image).enhance(rng.uniform(0.3, 1.9))
    if perturbation == "blur":
        return image.filter(ImageFilter.GaussianBlur(radius=rng.uniform(0.6, 2.2)))
    return image


def _collect_source_images(src_dir: Path, max_inputs: int) -> list[Path]:
    return sorted(
        (p for p in src_dir.rglob("*") if p.suffix.lower() in _IMAGE_SUFFIXES),
        key=lambda p: p.name,
    )[:max_inputs]


def reference_augment_frames(
    input_uri: str,
    output_uri: str,
    *,
    run_id: str = "",
    variants_per_frame: int = 2,
    max_inputs: int = 8,
) -> dict[str, Any]:
    """Produce real augmented image frames without the heavy Cosmos model.

    Downloads the source frames from ``input_uri``, applies genuine per-frame PIL
    augmentations (lighting / contrast / color / blur), and writes/uploads the
    augmented PNGs to ``output_uri`` so downstream stages (e.g. VLM critique) get
    real image frames instead of a descriptor stub. Used when the
    Cosmos-Transfer2.5 runtime image is not present; ``--execute`` runs the real
    model instead.

    ``s3://`` URIs are read/written via :class:`StorageClient`; any other value is
    treated as a local directory (keeps the function unit-testable without S3).
    """

    import json
    import tempfile

    from PIL import Image

    def _is_s3(uri: str) -> bool:
        return uri.strip().startswith("s3://")

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        out_dir = Path(tmp) / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        storage = None
        if _is_s3(input_uri) or _is_s3(output_uri):
            from npa.clients.storage import StorageClient

            storage = StorageClient.from_environment()

        if _is_s3(input_uri):
            assert storage is not None
            storage.download_directory(input_uri, str(src_dir))
        else:
            local_src = Path(input_uri.replace("local://", "").replace("file://", ""))
            if local_src.is_dir():
                for item in local_src.rglob("*"):
                    if item.is_file():
                        dest = src_dir / item.relative_to(local_src)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, dest)
            elif local_src.is_file():
                # A single local image file is a valid source too.
                shutil.copy2(local_src, src_dir / local_src.name)

        sources = _collect_source_images(src_dir, max_inputs)
        if not sources:
            raise RuntimeError(
                f"cosmos2 transfer: no source images found under {input_uri!r}; "
                "expected at least one .png/.jpg frame to augment."
            )

        if _is_s3(output_uri):
            frames_uri = output_uri
            dest_dir = None
        else:
            dest_dir = Path(output_uri.replace("local://", "").replace("file://", ""))
            # Preserve an explicit local scheme in every returned frame URI so
            # ``frames[].uri`` and ``index_uri`` use the same address space.
            # Plain filesystem inputs remain plain paths for compatibility.
            frames_uri = output_uri.rstrip("/")

        index: list[dict[str, Any]] = []
        frame_no = 0
        for src in sources:
            base = Image.open(src).convert("RGB")
            for variant in range(max(1, variants_per_frame)):
                perturbation = _PERTURBATIONS[frame_no % len(_PERTURBATIONS)]
                augmented = _apply_perturbation(base, perturbation, seed=frame_no)
                name = f"frame-{frame_no:05d}.png"
                augmented.save(out_dir / name)
                index.append(
                    {
                        "frame_id": f"frame-{frame_no:05d}",
                        "perturbation": perturbation,
                        "source": src.name,
                        "uri": f"{frames_uri.rstrip('/')}/{name}",
                        "variant": variant,
                    }
                )
                frame_no += 1

        (out_dir / AUGMENTED_FRAMES_INDEX).write_text(
            json.dumps(
                {
                    "schema": AUGMENTED_FRAMES_SCHEMA,
                    "run_id": run_id,
                    "frame_count": frame_no,
                    "frames": index,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        if _is_s3(output_uri):
            assert storage is not None
            storage.upload_directory(str(out_dir), output_uri)
        else:
            assert dest_dir is not None
            dest_dir.mkdir(parents=True, exist_ok=True)
            for item in out_dir.iterdir():
                shutil.copy2(item, dest_dir / item.name)

    return {
        "augmented_frames_uri": frames_uri,
        "frames": index,
        "index_uri": augmented_frames_index_uri_for(output_uri),
        "frame_count": frame_no,
        "source_frame_count": len(sources),
    }


__all__ = [
    "AUGMENTED_FRAMES_INDEX",
    "AUGMENTED_FRAMES_SCHEMA",
    "FrameExtractionError",
    "ProtectedChromaError",
    "REFERENCE_AUGMENT_MODE",
    "REFERENCE_AUGMENT_STATUS",
    "TRANSFER_MANIFEST_FILENAME",
    "TRANSFER_MANIFEST_MODE",
    "TRANSFER_MANIFEST_SCHEMA",
    "preserve_source_chroma",
    "TRANSFER_MANIFEST_STATUS",
    "augmented_frames_index_uri_for",
    "cosmos_transfer_available",
    "cosmos_transfer_repo",
    "ensure_env",
    "extract_frames",
    "publish_transfer_clip",
    "publish_transfer_to_s3",
    "reference_augment_frames",
    "run_cosmos_transfer",
    "transfer_manifest_uri_for",
    "write_run_manifest",
]
