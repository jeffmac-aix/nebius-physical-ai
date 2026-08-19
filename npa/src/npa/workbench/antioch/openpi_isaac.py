"""Isaac Lab Franka/camera runtime for the OpenPI bridge.

This module is started only through ``/isaac-sim/python.sh``.  It may also be
called from an Antioch-authored scenario after ``antioch.boot()``; the robot,
cameras, and position-control path remain native Isaac Lab in both cases.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import urlparse

import numpy as np

from .openpi_bridge import (
    ACTION_SHAPE,
    OpenPIBridgeError,
    OpenPIWebsocketClient,
    safe_position_targets,
)


def _verify_vulkan_runtime() -> None:
    """Fail before Kit startup when the host exposed only CUDA driver userspace."""

    configured_icds = os.environ.get("VK_ICD_FILENAMES", "").split(":")
    icd_paths = [Path(value) for value in configured_icds if value]
    if not icd_paths:
        icd_paths = [
            Path("/etc/vulkan/icd.d/nvidia_icd.json"),
            Path("/usr/share/vulkan/icd.d/nvidia_icd.json"),
        ]
    if not any(path.is_file() for path in icd_paths):
        raise OpenPIBridgeError(
            "NVIDIA Vulkan ICD is unavailable; refusing to start Isaac without "
            "the host graphics driver capability"
        )
    vulkaninfo = shutil.which("vulkaninfo")
    if not vulkaninfo:
        raise OpenPIBridgeError(
            "vulkaninfo is unavailable; cannot prove Isaac rendering readiness"
        )
    try:
        probe = subprocess.run(
            [vulkaninfo, "--summary"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise OpenPIBridgeError("Vulkan readiness probe timed out") from exc
    output = probe.stdout + probe.stderr
    if probe.returncode != 0 or "NVIDIA" not in output.upper():
        raise OpenPIBridgeError(
            "Vulkan readiness probe did not find an NVIDIA renderer; refusing "
            "non-render fallback"
        )


def _ensure_franka_asset_root(assets: Any | None = None) -> str:
    """Use the latest published NVIDIA asset root when a newer SDK points at 404s."""

    if assets is None:
        import isaaclab.utils.assets as assets

    root = str(assets.NUCLEUS_ASSET_ROOT_DIR).rstrip("/")
    match = re.search(r"/Assets/Isaac/([^/]+)$", root)
    if not root.startswith("https://") or match is None:
        return "native"
    sentinel = f"{root}/Isaac/IsaacLab/Robots/FrankaEmika/panda_instanceable.usd"

    def available(url: str) -> bool:
        request = urllib.request.Request(url, method="HEAD")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return int(response.status) == 200
        except (OSError, urllib.error.HTTPError):
            return False

    if available(sentinel):
        return "native"
    # Antioch's Isaac Sim 6.0 engine currently advertises an asset prefix before
    # that prefix is published. NVIDIA's immutable 5.1 Franka USD is compatible
    # with the articulation/camera API used here; prove it exists before changing
    # any module constants so a network outage still fails closed.
    fallback_root = root[: match.start(1)] + "5.1"
    fallback_sentinel = sentinel.replace(root, fallback_root, 1)
    if not available(fallback_sentinel):
        raise OpenPIBridgeError(
            "NVIDIA Franka asset is unavailable at both the native and reviewed "
            "compatibility roots"
        )
    for name, value in vars(assets).items():
        if name.endswith("_DIR") and isinstance(value, str) and value.startswith(root):
            setattr(assets, name, fallback_root + value[len(root) :])
    return "nvidia-5.1-compatibility"


def _compatible_franka_asset_url(url: str, asset_compatibility: str) -> str:
    if asset_compatibility == "nvidia-5.1-compatibility":
        return url.replace("/Assets/Isaac/6.0/", "/Assets/Isaac/5.1/", 1)
    return url


def _write_report(uri: str, report: dict[str, object]) -> None:
    if not uri:
        print(
            "NPA_OPENPI_BRIDGE_RESULT=" + json.dumps(report, sort_keys=True), flush=True
        )
        return
    payload = json.dumps(report, indent=2, sort_keys=True).encode() + b"\n"
    parsed = urlparse(uri)
    if parsed.scheme == "s3":
        import boto3

        boto3.client(
            "s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None
        ).put_object(
            Bucket=parsed.netloc,
            Key=parsed.path.lstrip("/"),
            Body=payload,
            ContentType="application/json",
            IfNoneMatch="*",
        )
        return
    path = Path(parsed.path if parsed.scheme == "file" else uri)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _camera_frame(image: Any) -> Any:
    value = image[0] if image.ndim == 4 else image
    if value.ndim != 3 or value.shape[-1] < 3:
        raise OpenPIBridgeError(
            f"camera returned invalid RGB shape {tuple(image.shape)}"
        )
    return value[:, :, :3]


def _resize_rgb(image: Any) -> np.ndarray:
    import torch

    value = _camera_frame(image).permute(2, 0, 1).unsqueeze(0).float()
    resized = torch.nn.functional.interpolate(
        value, size=(224, 224), mode="bilinear", align_corners=False
    )
    return np.clip(resized[0].permute(1, 2, 0).cpu().numpy(), 0, 255).astype(np.uint8)


def run(*, launch_application: bool = True) -> dict[str, object]:
    """Capture two cameras, request one chunk, and apply five safe targets.

    Antioch's scenario runner already owns Kit startup, so an authored scenario
    passes ``launch_application=False``.  The Kubernetes image owns its process
    and uses the default standalone launcher.
    """

    env = None
    client = None
    simulation_app = None
    report: dict[str, object]
    try:
        if launch_application:
            _verify_vulkan_runtime()
            from isaaclab.app import AppLauncher

            simulation_app = AppLauncher(headless=True, enable_cameras=True).app
        import gymnasium as gym
        import isaaclab.sim as sim_utils
        from isaaclab.utils import assets as asset_utils

        asset_compatibility = _ensure_franka_asset_root(asset_utils)
        import isaaclab_tasks  # noqa: F401
        import torch
        from isaaclab.sensors import CameraCfg
        from isaaclab_tasks.utils import parse_env_cfg

        from npa.workflows.isaac_capture import look_at_quaternion

        client = OpenPIWebsocketClient(
            os.environ.get("OPENPI_POLICY_HOST", ""),
            port=int(os.environ.get("OPENPI_POLICY_PORT", "8000")),
            connect_timeout_seconds=float(
                os.environ.get("OPENPI_CONNECT_TIMEOUT_SECONDS", "10")
            ),
            inference_timeout_seconds=float(
                os.environ.get("OPENPI_INFERENCE_TIMEOUT_SECONDS", "60")
            ),
        )
        if not torch.cuda.is_available():
            raise OpenPIBridgeError("CUDA is unavailable; refusing non-render fallback")
        properties = torch.cuda.get_device_properties(0)
        capability = f"{properties.major}.{properties.minor}"
        if capability != "12.0":
            raise OpenPIBridgeError(
                f"Isaac bridge requires RTX PRO 6000 sm_120, received compute capability {capability}"
            )
        cfg = parse_env_cfg("Isaac-Lift-Cube-Franka-v0", device="cuda:0", num_envs=1)
        cfg.scene.robot.spawn.usd_path = _compatible_franka_asset_url(
            cfg.scene.robot.spawn.usd_path, asset_compatibility
        )
        cfg.scene.npa_exterior_camera = CameraCfg(
            prim_path="{ENV_REGEX_NS}/NpaExteriorCamera",
            offset=CameraCfg.OffsetCfg(
                pos=(1.4, 1.4, 1.2),
                rot=look_at_quaternion((1.4, 1.4, 1.2), (0.5, 0.0, 0.6)),
                convention="world",
            ),
            data_types=["rgb"],
            width=320,
            height=320,
            spawn=sim_utils.PinholeCameraCfg(focal_length=24.0),
        )
        cfg.scene.npa_wrist_camera = CameraCfg(
            prim_path="{ENV_REGEX_NS}/Robot/panda_hand/NpaWristCamera",
            offset=CameraCfg.OffsetCfg(
                pos=(0.08, 0.0, 0.02),
                rot=(0.7071068, 0.0, 0.7071068, 0.0),
                convention="ros",
            ),
            data_types=["rgb"],
            width=320,
            height=320,
            spawn=sim_utils.PinholeCameraCfg(focal_length=18.0),
        )
        env = gym.make("Isaac-Lift-Cube-Franka-v0", cfg=cfg)
        env.reset()
        uenv = env.unwrapped
        robot = uenv.scene["robot"]
        for _ in range(4):
            uenv.scene.write_data_to_sim()
            uenv.sim.step(render=True)
            uenv.scene.update(uenv.sim.get_physics_dt())
        joint_position = robot.data.joint_pos[0, :7].detach().cpu().numpy()
        finger = robot.data.joint_pos[0, 7:9].mean().item()
        gripper = np.asarray([np.clip(finger / 0.04, 0.0, 1.0)], dtype=np.float32)
        observation = {
            "observation/exterior_image_1_left": _resize_rgb(
                uenv.scene["npa_exterior_camera"].data.output["rgb"]
            ),
            "observation/wrist_image_left": _resize_rgb(
                uenv.scene["npa_wrist_camera"].data.output["rgb"]
            ),
            "observation/joint_position": joint_position.astype(np.float32),
            "observation/gripper_position": gripper,
            "prompt": os.environ.get("OPENPI_PROMPT", "pick up the fork"),
        }
        actions = client.infer(observation)
        targets = safe_position_targets(
            actions,
            joint_position,
            max_joint_delta_rad=float(
                os.environ.get("OPENPI_MAX_JOINT_DELTA_RAD", "0.08")
            ),
            execute_steps=int(os.environ.get("OPENPI_EXECUTE_STEPS", "5")),
        )
        executed = 0
        for target in targets:
            fingers = np.repeat(float(target[7]) * 0.04, 2)
            full_target = torch.as_tensor(
                np.concatenate([target[:7], fingers]),
                device=robot.device,
                dtype=robot.data.joint_pos.dtype,
            ).unsqueeze(0)
            robot.set_joint_position_target(full_target)
            uenv.scene.write_data_to_sim()
            uenv.sim.step(render=True)
            uenv.scene.update(uenv.sim.get_physics_dt())
            executed += 1
        report = {
            "schema": "npa.antioch.openpi-franka-bridge.v1",
            "status": "passed",
            "simulator": "isaac-lab",
            "antioch_compatible": True,
            "gpu_compute_capability": capability,
            "asset_root": asset_compatibility,
            "camera_shapes": [[224, 224, 3], [224, 224, 3]],
            "policy_action_shape": list(ACTION_SHAPE),
            "targets_executed": executed,
            "position_control": "absolute-rate-limited",
            "policy_transport": "private-openpi-websocket",
            "fail_closed": True,
        }
    except Exception as exc:
        report = {
            "schema": "npa.antioch.openpi-franka-bridge.v1",
            "status": "failed-no-action",
            "error_type": type(exc).__name__,
            "targets_executed": 0,
            "fail_closed": True,
        }
        _write_report(os.environ.get("NPA_OPENPI_BRIDGE_OUTPUT_URI", ""), report)
        raise
    finally:
        if client is not None:
            client.close()
        if env is not None:
            env.close()
        # Keep the launcher-owned app reachable until after environment cleanup.
        _ = simulation_app
    _write_report(os.environ.get("NPA_OPENPI_BRIDGE_OUTPUT_URI", ""), report)
    return report


def main() -> int:
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
