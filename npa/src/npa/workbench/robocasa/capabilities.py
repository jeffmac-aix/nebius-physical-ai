"""Real RoboCasa capability operations.

This module is the single source of truth for RoboCasa capability behavior. The
FastAPI service, the CLI, and the SDK all call into it. It exercises the real
upstream RoboCasa surface: Gymnasium task registration, kitchen asset
availability, headless EGL environment reset, and a random rollout with a video
artifact.

GPU-heavy imports (robocasa, robosuite, mujoco, gymnasium) are deferred to call
time so that importing this module on a client without the simulation stack
never fails.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
from pathlib import Path
from typing import Any

from npa.workbench.robocasa.schemas import (
    DEFAULT_ENV_ID,
    RoboCasaRunRequest,
    RoboCasaSystemInfo,
)

LOGGER = logging.getLogger(__name__)

#: Capabilities this tool can exercise, keyed by the upstream capability id.
SUPPORTED_CAPABILITIES = {
    "kitchen_task_registration",
    "kitchen_asset_availability",
    "kitchen_egl_env_reset",
    "kitchen_random_rollout",
}


class RoboCasaError(RuntimeError):
    """Raised when a RoboCasa capability operation fails."""


def make_run_id(capability: str, manifest: str) -> str:
    """Build a deterministic run id from a capability and request manifest."""
    digest = hashlib.sha256(f"{capability}:{manifest}".encode("utf-8")).hexdigest()[:12]
    return f"robocasa-{capability}-{digest}"


def compute_manifest_sha256(capability: str, payload: dict[str, Any]) -> str:
    """Compute a content hash over a request payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{capability}:{canonical}".encode("utf-8")).hexdigest()


def _import_robocasa() -> Any:
    """Import the real robocasa package, raising a clear error if absent."""
    try:
        import robocasa  # noqa: F401
    except Exception as exc:  # pragma: no cover - depends on the container.
        raise RoboCasaError(
            "robocasa is not installed in this environment; run inside the "
            "npa-robocasa image"
        ) from exc
    return robocasa


def _import_gymnasium() -> Any:
    try:
        import gymnasium as gym
    except Exception as exc:  # pragma: no cover - depends on the container.
        raise RoboCasaError("gymnasium is not installed in this environment") from exc
    return gym


def _assets_root() -> Path:
    robocasa = _import_robocasa()
    return Path(robocasa.__file__).resolve().parent / "models" / "assets"


def _package_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:  # pragma: no cover - best effort.
        return ""


def system_info() -> RoboCasaSystemInfo:
    """Collect system and RoboCasa stack information."""
    info = RoboCasaSystemInfo(
        status="ok",
        python=platform.python_version(),
        platform=platform.platform(),
        robocasa_version=_package_version("robocasa"),
        robosuite_version=_package_version("robosuite"),
        mujoco_version=_package_version("mujoco"),
        gymnasium_version=_package_version("gymnasium"),
    )
    try:
        import torch

        info.cuda_available = bool(torch.cuda.is_available())
        info.cuda_device_count = int(torch.cuda.device_count()) if torch.cuda.is_available() else 0
        if torch.cuda.is_available():
            info.cuda_device_name = torch.cuda.get_device_name(0)
    except Exception as exc:  # pragma: no cover - torch optional on client.
        LOGGER.debug("torch unavailable: %s", exc)
    try:
        gym = _import_gymnasium()
        robocasa_envs = sorted(
            env for env in gym.envs.registry.keys() if env.startswith("robocasa/")
        )
        info.registered_env_count = len(robocasa_envs)
    except Exception as exc:  # pragma: no cover - depends on the container.
        LOGGER.debug("gymnasium unavailable: %s", exc)
    try:
        info.assets_root_exists = _assets_root().exists()
    except Exception as exc:  # pragma: no cover - depends on the container.
        LOGGER.debug("assets root unavailable: %s", exc)
    return info


def kitchen_task_registration(*, env_id: str = DEFAULT_ENV_ID) -> dict[str, Any]:
    """Verify Gymnasium task registration for a RoboCasa env id."""
    gym = _import_gymnasium()
    if env_id not in gym.envs.registry:
        raise RoboCasaError(f"RoboCasa env id not registered: {env_id}")
    spec = gym.envs.registry[env_id]
    robocasa_envs = sorted(
        env for env in gym.envs.registry.keys() if env.startswith("robocasa/")
    )
    return {
        "env_id": env_id,
        "entry_point": str(spec.entry_point),
        "registered_env_count": len(robocasa_envs),
        "sample_registered_envs": robocasa_envs[:10],
    }


def kitchen_asset_availability() -> dict[str, Any]:
    """Verify the kitchen assets root exists and is populated."""
    assets_root = _assets_root()
    if not assets_root.exists():
        raise RoboCasaError(f"RoboCasa assets root does not exist: {assets_root}")
    subdirs = sorted(
        p.name for p in assets_root.iterdir() if p.is_dir()
    )
    return {
        "assets_root": str(assets_root),
        "assets_root_exists": True,
        "subdirs": subdirs,
    }


def kitchen_egl_env_reset(*, env_id: str = DEFAULT_ENV_ID, seed: int | None = None) -> dict[str, Any]:
    """Create a headless EGL RoboCasa env and reset it."""
    os.environ.setdefault("MUJOCO_GL", "egl")
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    gym = _import_gymnasium()
    try:
        env = gym.make(env_id)
    except Exception as exc:  # pragma: no cover - depends on the container.
        raise RoboCasaError(f"failed to create RoboCasa env {env_id}: {exc}") from exc
    try:
        obs, info = env.reset(seed=seed)
        return {
            "env_id": env_id,
            "reset_ok": True,
            "observation_keys": sorted(obs.keys()) if isinstance(obs, dict) else [],
            "info_keys": sorted(info.keys()) if isinstance(info, dict) else [],
            "mujoco_gl": os.environ.get("MUJOCO_GL", ""),
        }
    except Exception as exc:  # pragma: no cover - depends on the container.
        raise RoboCasaError(f"failed to reset RoboCasa env {env_id}: {exc}") from exc
    finally:
        try:
            env.close()
        except Exception as exc:  # pragma: no cover - best effort.
            LOGGER.debug("env close failed: %s", exc)


def kitchen_random_rollout(
    *,
    env_id: str = DEFAULT_ENV_ID,
    iterations: int = 1,
    seed: int | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Run a real random rollout and write a video artifact."""
    os.environ.setdefault("MUJOCO_GL", "egl")
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    gym = _import_gymnasium()
    try:
        env = gym.make(env_id)
    except Exception as exc:  # pragma: no cover - depends on the container.
        raise RoboCasaError(f"failed to create RoboCasa env {env_id}: {exc}") from exc
    video_path: Path | None = None
    try:
        obs, _ = env.reset(seed=seed)
        frames: list[Any] = []
        for _ in range(iterations):
            action = env.action_space.sample()
            obs, reward, terminated, truncated, info = env.step(action)
            try:
                frames.append(env.render())
            except Exception as exc:  # pragma: no cover - render may be unavailable.
                LOGGER.debug("render unavailable: %s", exc)
            if terminated or truncated:
                break
        result: dict[str, Any] = {
            "env_id": env_id,
            "rollout_ok": True,
            "iterations": iterations,
            "final_reward": float(reward),
            "terminated": bool(terminated),
            "truncated": bool(truncated),
            "observation_keys": sorted(obs.keys()) if isinstance(obs, dict) else [],
        }
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            video_path = _write_video(frames, output_dir / "rollout.mp4")
            if video_path is not None:
                result["video_exists"] = True
                result["video_bytes"] = video_path.stat().st_size
                result["video_sha256"] = _sha256_file(video_path)
        return result
    except Exception as exc:  # pragma: no cover - depends on the container.
        raise RoboCasaError(f"failed to run RoboCasa rollout {env_id}: {exc}") from exc
    finally:
        try:
            env.close()
        except Exception as exc:  # pragma: no cover - best effort.
            LOGGER.debug("env close failed: %s", exc)


def _write_video(frames: list[Any], path: Path) -> Path | None:
    """Write frames to an MP4 using imageio's ffmpeg backend when available."""
    if not frames:
        return None
    try:
        import imageio

        imageio.mimsave(path, frames, fps=20)
        return path
    except Exception:  # pragma: no cover - ffmpeg backend may be absent.
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_capability(
    request: RoboCasaRunRequest,
    *,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Dispatch a RoboCasa capability request to the real implementation."""
    if request.capability == "kitchen_task_registration":
        return kitchen_task_registration(env_id=request.env_id)
    if request.capability == "kitchen_asset_availability":
        return kitchen_asset_availability()
    if request.capability == "kitchen_egl_env_reset":
        return kitchen_egl_env_reset(env_id=request.env_id, seed=request.seed)
    if request.capability == "kitchen_random_rollout":
        return kitchen_random_rollout(
            env_id=request.env_id,
            iterations=request.iterations,
            seed=request.seed,
            output_dir=output_dir,
        )
    raise RoboCasaError(f"unsupported robocasa capability: {request.capability}")


__all__ = [
    "SUPPORTED_CAPABILITIES",
    "RoboCasaError",
    "compute_manifest_sha256",
    "kitchen_asset_availability",
    "kitchen_egl_env_reset",
    "kitchen_random_rollout",
    "kitchen_task_registration",
    "make_run_id",
    "run_capability",
    "system_info",
]
