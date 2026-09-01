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

import numpy as np
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
    "kitchen_trajectory_export",
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



def _download_assets() -> None:
    """Download the RoboCasa kitchen assets (textures, fixtures, objects).

    Assets are NOT baked into the image and download at runtime from the
    operator's entitled Hugging Face identity. This mirrors the upstream
    ``download_kitchen_assets.py`` registry but skips its interactive prompt so
    it can run inside the service. Missing assets are the usual cause of a
    ``model.xml`` FileNotFoundError on the first real rollout.

    The standard fixtures (stoves, windows, sinks, ...) live in
    ``robocasa/robocasa-assets/fixtures.zip``; the lightwheel variants are
    published as individual ``fixtures_lightwheel/<name>.zip`` files in
    ``nvidia/PhysicalAI-Kitchen-Assets``.
    """
    try:
        from huggingface_hub import hf_hub_download
        from zipfile import ZipFile
        import robocasa
        from pathlib import Path as _Path

        assets_root = _Path(robocasa.__file__).resolve().parent / "models" / "assets"
        # (repo_id, filename, extract_to) where extract_to is relative to assets_root.
        registry = [
            ("robocasa/robocasa-assets", "textures.zip", "."),
            ("robocasa/robocasa-assets", "generative_textures.zip", "."),
            ("robocasa/robocasa-assets", "fixtures.zip", "."),
            ("robocasa/robocasa-assets", "objaverse.zip", "."),
            ("robocasa/robocasa-assets", "aigen_objs.zip", "."),
        ]
        # Lightwheel fixtures are one zip per fixture family.
        lightwheel_fixtures = [
            "blenders", "cabinets", "coffee_machines", "dishwashers",
            "electric_kettles", "fridges", "handles", "hoods", "microwaves",
            "ovens", "sinks", "stand_mixers", "stoves", "stovetops",
            "toaster_ovens", "toasters", "windows",
        ]
        for name in lightwheel_fixtures:
            registry.append(
                ("nvidia/PhysicalAI-Kitchen-Assets", f"fixtures_lightwheel/{name}.zip", "fixtures")
            )
        for repo_id, filename, extract_to in registry:
            target = assets_root / extract_to
            if extract_to != "." and target.exists() and any(target.iterdir()):
                continue
            try:
                zip_path = hf_hub_download(
                    repo_id=repo_id,
                    repo_type="dataset",
                    filename=filename,
                    revision="main",
                )
                assets_root.mkdir(parents=True, exist_ok=True)
                with ZipFile(zip_path, "r") as zf:
                    zf.extractall(path=assets_root)
                LOGGER.info("downloaded robocasa assets %s from %s", filename, repo_id)
            except Exception as exc:  # pragma: no cover - network/entitlement.
                LOGGER.warning("failed to download robocasa assets %s: %s", filename, exc)
    except Exception as exc:  # pragma: no cover - client without the stack.
        LOGGER.warning("robocasa asset download unavailable: %s", exc)


def _make_env(env_id: str, *, download_assets: bool = True) -> Any:
    """Create a headless EGL RoboCasa env, downloading assets when requested.

    The upstream gym wrapper defaults ``split="test"``, which the pinned
    ``create_env`` rejects; pass ``split="all"`` so real rollouts can run.
    """
    os.environ.setdefault("MUJOCO_GL", "egl")
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    if download_assets:
        _download_assets()
    gym = _import_gymnasium()
    try:
        return gym.make(env_id, split="all")
    except Exception as exc:  # pragma: no cover - depends on the container.
        raise RoboCasaError(f"failed to create RoboCasa env {env_id}: {exc}") from exc

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


def kitchen_egl_env_reset(
    *, env_id: str = DEFAULT_ENV_ID, seed: int | None = None, download_assets: bool = True
) -> dict[str, Any]:
    """Create a headless EGL RoboCasa env and reset it."""
    env = _make_env(env_id, download_assets=download_assets)
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
    download_assets: bool = True,
) -> dict[str, Any]:
    """Run a real random rollout and write a video artifact."""
    env = _make_env(env_id, download_assets=download_assets)
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


def kitchen_trajectory_export(
    *,
    env_id: str = DEFAULT_ENV_ID,
    iterations: int = 1,
    num_envs: int = 1,
    seed: int | None = None,
    output_dir: Path | None = None,
    download_assets: bool = True,
) -> dict[str, Any]:
    """Run real RoboCasa rollouts and export trajectories for LeRobotDataset.

    Writes one ``episode_NNNN/`` directory per rollout, each containing the
    numpy arrays the ``npa adapter convert`` adapter consumes:

      obs_workspace.npy  (T, H, W, 3) uint8   workspace camera
      obs_wrist.npy      (T, H, W, 3) uint8   wrist camera
      state.npy          (T, n_joints) float32
      actions.npy        (T, n_actions) float32

    plus a per-episode ``rollout.mp4`` and run-level ``metadata.json`` /
    ``metrics.json``. This is the real trajectory export seam between RoboCasa
    simulation and LeRobotDataset policy training.
    """
    env = _make_env(env_id, download_assets=download_assets)
    try:
        episodes: list[dict[str, Any]] = []
        for ep in range(num_envs):
            obs, _ = env.reset(seed=(seed + ep) if seed is not None else None)
            workspace_frames: list[Any] = []
            wrist_frames: list[Any] = []
            states: list[Any] = []
            actions: list[Any] = []
            reward = 0.0
            terminated = False
            truncated = False
            for _ in range(iterations):
                action = env.action_space.sample()
                obs, reward, terminated, truncated, _info = env.step(action)
                workspace_frames.append(_obs_image(obs, "agentview_image"))
                wrist_frames.append(_obs_image(obs, "eye_in_hand_image"))
                states.append(_obs_state(obs))
                actions.append(np.asarray(action, dtype=np.float32))
                if terminated or truncated:
                    break
            if output_dir is not None:
                ep_dir = output_dir / f"episode_{ep:04d}"
                ep_dir.mkdir(parents=True, exist_ok=True)
                np.save(ep_dir / "obs_workspace.npy", np.stack(workspace_frames))
                np.save(ep_dir / "obs_wrist.npy", np.stack(wrist_frames))
                np.save(ep_dir / "state.npy", np.stack(states))
                np.save(ep_dir / "actions.npy", np.stack(actions))
                _write_video(workspace_frames, ep_dir / "rollout.mp4")
            episodes.append(
                {
                    "episode_index": ep,
                    "length": len(actions),
                    "final_reward": float(reward),
                    "terminated": bool(terminated),
                    "truncated": bool(truncated),
                }
            )
        result: dict[str, Any] = {
            "env_id": env_id,
            "trajectory_export_ok": True,
            "num_episodes": len(episodes),
            "iterations": iterations,
            "episodes": episodes,
        }
        if output_dir is not None:
            _write_run_metadata(output_dir, env_id, episodes)
            result["output_dir"] = str(output_dir)
        return result
    except Exception as exc:  # pragma: no cover - depends on the container.
        raise RoboCasaError(
            f"failed to run RoboCasa trajectory export {env_id}: {exc}"
        ) from exc
    finally:
        try:
            env.close()
        except Exception as exc:  # pragma: no cover - best effort.
            LOGGER.debug("env close failed: %s", exc)


def _obs_image(obs: dict[str, Any], key: str) -> Any:
    """Return a uint8 (H, W, 3) image frame for a RoboCasa observation key."""
    frame = obs.get(key)
    if frame is None:
        raise RoboCasaError(f"RoboCasa observation missing image key: {key}")
    arr = np.asarray(frame)
    if arr.ndim == 3 and arr.shape[2] == 3:
        return arr.astype(np.uint8)
    if arr.ndim == 4 and arr.shape[0] == 1:
        return arr[0].astype(np.uint8)
    raise RoboCasaError(
        f"RoboCasa image key {key!r} has unexpected shape {arr.shape}"
    )


def _obs_state(obs: dict[str, Any]) -> np.ndarray:
    """Build a float32 robot-state vector from a RoboCasa observation."""
    parts: list[np.ndarray] = []
    for key in ("robot0_joint_pos", "robot0_eef_pos", "robot0_gripper_qpos"):
        value = obs.get(key)
        if value is not None:
            parts.append(np.asarray(value, dtype=np.float32).reshape(-1))
    if not parts:
        raise RoboCasaError("RoboCasa observation has no robot state keys")
    return np.concatenate(parts)


def _write_run_metadata(
    output_dir: Path, env_id: str, episodes: list[dict[str, Any]]
) -> None:
    """Write run-level metadata.json and metrics.json for the trajectory export."""
    metadata = {
        "env_id": env_id,
        "num_episodes": len(episodes),
        "episodes": episodes,
        "format": "lerobot-adapter-input",
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True)
    )
    metrics = {
        "num_episodes": len(episodes),
        "total_steps": sum(int(ep["length"]) for ep in episodes),
        "mean_episode_length": (
            sum(int(ep["length"]) for ep in episodes) / len(episodes)
            if episodes
            else 0.0
        ),
    }
    (output_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True)
    )


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
        return kitchen_egl_env_reset(
            env_id=request.env_id, seed=request.seed, download_assets=request.download_assets
        )
    if request.capability == "kitchen_random_rollout":
        return kitchen_random_rollout(
            env_id=request.env_id,
            iterations=request.iterations,
            seed=request.seed,
            output_dir=output_dir,
            download_assets=request.download_assets,
        )
    if request.capability == "kitchen_trajectory_export":
        return kitchen_trajectory_export(
            env_id=request.env_id,
            iterations=request.iterations,
            num_envs=request.num_envs,
            seed=request.seed,
            output_dir=output_dir,
            download_assets=request.download_assets,
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
    "kitchen_trajectory_export",
    "make_run_id",
    "run_capability",
    "system_info",
]
