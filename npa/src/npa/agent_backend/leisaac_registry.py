"""Authoritative registry for browser-teleoperable LeIsaac environments.

Keep this module dependency-free: the agent bootstrap and the LeIsaac container
ship the same source file so the CLI, runtime, manifest validation, and UI all
use identical task metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

REGISTRY_SCHEMA = "npa.leisaac.task-registry.v1"
DEFAULT_TASK = "LeIsaac-SO101-PickOrange-v0"
DEFAULT_ENVIRONMENT_ID = "operator-0"
TELEOP_DEVICE = "keyboard"

_ENVIRONMENT_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")

RUNTIME_ASSETS: tuple[dict[str, str], ...] = (
    {
        "id": "so101_follower",
        "url": "https://github.com/LightwheelAI/leisaac/releases/download/v0.1.0/so101_follower.usd",
        "sha256": "64a877c3b82cdc4a48ab8a1f321a2dd3ef7c55d4b10bce222b58c530d978ae58",
        "destination": "robots/so101_follower.usd",
        "archive": "false",
    },
    {
        "id": "kitchen_with_orange",
        "url": "https://github.com/LightwheelAI/leisaac/releases/download/v0.1.0/kitchen_with_orange.zip",
        "sha256": "d314c54b63a17e91402bfaddf26e21ff614adf2430fa092b78897f15b8adea34",
        "destination": "scenes/kitchen_with_orange/scene.usd",
        "archive": "true",
    },
    {
        "id": "table_with_cube",
        "url": "https://github.com/LightwheelAI/leisaac/releases/download/v0.1.2/table_with_cube.zip",
        "sha256": "917c66a724019d235cc9f442a30ae72e5663b44ef4ed8d4d5324e549e11952b7",
        "destination": "scenes/table_with_cube/scene.usd",
        "archive": "true",
    },
)

# This is intentionally smaller than upstream's complete Gym registry. These
# are the single-arm SO101 tasks at the pinned source commit that accept the
# exact eight-dimensional SO101Keyboard action path used by the browser relay.
SUPPORTED_TASKS: tuple[dict[str, Any], ...] = (
    {
        "task": "LeIsaac-SO101-PickOrange-v0",
        "display_name": "SO101 Pick Orange",
        "description": "Pick an orange from the kitchen counter and place it on the plate.",
        "robot": "SO101",
        "teleop_device": TELEOP_DEVICE,
        "action_dimension": 8,
        "state_joint_names": [
            "shoulder_pan",
            "shoulder_lift",
            "elbow_flex",
            "wrist_flex",
            "wrist_roll",
            "gripper",
        ],
        "asset_ids": ["so101_follower", "kitchen_with_orange"],
    },
    {
        "task": "LeIsaac-SO101-LiftCube-v0",
        "display_name": "SO101 Lift Cube",
        "description": "Grasp and lift the red cube from the table.",
        "robot": "SO101",
        "teleop_device": TELEOP_DEVICE,
        "action_dimension": 8,
        "state_joint_names": [
            "shoulder_pan",
            "shoulder_lift",
            "elbow_flex",
            "wrist_flex",
            "wrist_roll",
            "gripper",
        ],
        "asset_ids": ["so101_follower", "table_with_cube"],
    },
)


def registry_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": REGISTRY_SCHEMA,
        "source": {
            "version": "0.4.0",
            "commit": "1651c321e9b0c1bb54233211fc7b3cd70d8373d5",
        },
        "default_task": DEFAULT_TASK,
        "environment_model": "named-sequential",
        "max_parallel_environments": 1,
        "tasks": [dict(item) for item in SUPPORTED_TASKS],
        "runtime_assets": [dict(item) for item in RUNTIME_ASSETS],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["fingerprint"] = hashlib.sha256(canonical).hexdigest()
    return payload


REGISTRY_FINGERPRINT = registry_payload()["fingerprint"]


def task_metadata(task: str) -> dict[str, Any]:
    value = str(task or "").strip()
    for item in SUPPORTED_TASKS:
        if item["task"] == value:
            return dict(item)
    allowed = ", ".join(item["task"] for item in SUPPORTED_TASKS)
    raise ValueError(f"unsupported LeIsaac task {value!r}; choose one of: {allowed}")


def validate_task(task: str) -> str:
    return str(task_metadata(task)["task"])


def validate_environment_id(environment_id: str) -> str:
    value = str(environment_id or "").strip()
    if not _ENVIRONMENT_ID.fullmatch(value):
        raise ValueError(
            "environment id must start with a letter or number and contain only "
            "letters, numbers, '.', '_' and '-', end with a letter or number, "
            "and contain at most 63 characters"
        )
    return value


def validate_environment_index(environment_index: int) -> int:
    value = int(environment_index)
    if value < 0 or value > 2**31 - 1:
        raise ValueError("environment index must be between 0 and 2147483647")
    return value


def validate_seed(seed: int) -> int:
    value = int(seed)
    if value < 0 or value > 2**32 - 1:
        raise ValueError("seed must be between 0 and 4294967295")
    return value


def validate_num_envs(num_envs: int) -> int:
    value = int(num_envs)
    if value != 1:
        raise ValueError(
            "browser teleoperation supports exactly one active environment per "
            "session; use distinct environment IDs across sequential launches"
        )
    return value
