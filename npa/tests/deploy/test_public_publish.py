"""License-guarded public-registry publishing.

Nebius CR has no anonymous/public mode, so public exposure means mirroring the
OSS-redistributable image subset to a public registry. These tests lock the
license boundary: the Omniverse-Kit images (isaac-lab, sonic, groot,
sonic-mujoco) must never be selected for a public registry, and the selector
must stay in sync with the packaging contract's redistribution classification.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from npa.deploy.images import (
    CONTAINER_IMAGE_NAMES,
    DEFAULT_PUBLIC_CONTAINER_REGISTRY,
    OMNIVERSE_RESTRICTED_TOOLS,
    is_publicly_redistributable,
    public_container_registry,
    publicly_publishable_tools,
)
from npa.deploy.publish_public import build_publish_plan

ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = ROOT / "npa" / "docker" / "workbench" / "packaging-contract.yaml"

# Contract image keys that are not canonical CONTAINER_IMAGE_NAMES tool keys.
# sonic-mujoco is a sonic variant, so the "sonic" restriction covers it.
CONTRACT_ALIASES = {"sonic-mujoco": "sonic"}


def _contract() -> dict:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_restricted_tools_are_the_omniverse_images() -> None:
    assert OMNIVERSE_RESTRICTED_TOOLS == frozenset({"isaac-lab", "sonic", "groot"})


def test_public_set_excludes_every_restricted_tool() -> None:
    public = set(publicly_publishable_tools())
    assert public.isdisjoint(OMNIVERSE_RESTRICTED_TOOLS)
    for tool in OMNIVERSE_RESTRICTED_TOOLS:
        assert not is_publicly_redistributable(tool)


def test_public_set_includes_the_oss_tools() -> None:
    public = set(publicly_publishable_tools())
    for tool in ("lerobot", "genesis", "cosmos", "fiftyone", "lancedb", "rerun-viewer", "retargeting"):
        assert tool in public, tool
    # Everything not Omniverse-restricted is public.
    assert public == set(CONTAINER_IMAGE_NAMES) - OMNIVERSE_RESTRICTED_TOOLS


def test_publish_plan_never_targets_a_restricted_image() -> None:
    plan = build_publish_plan(target_registry="ghcr.io/example/workbench")
    names = {item.source_ref.rsplit("/", 1)[-1].split(":", 1)[0] for item in plan}
    for restricted in ("npa-isaac-lab", "npa-sonic", "npa-groot", "npa-sonic-mujoco"):
        assert restricted not in names
    # Targets are all under the requested public registry.
    for item in plan:
        assert item.target_ref.startswith("ghcr.io/example/workbench/")


def test_publish_plan_requires_a_target() -> None:
    with pytest.raises(ValueError):
        build_publish_plan(target_registry="")


def test_publish_plan_copies_the_pinned_tag_unchanged() -> None:
    """A mirror must not retag: the public image has to be the same name:tag as
    the source so a consumer switching NPA_REGISTRY resolves the same reference.
    """
    plan = build_publish_plan(
        target_registry="ghcr.io/example/workbench",
        source_registry="cr.eu-north1.nebius.cloud/example",
    )
    for item in plan:
        assert item.source_ref.startswith("cr.eu-north1.nebius.cloud/example/")
        assert item.source_ref.rsplit("/", 1)[-1] == item.target_ref.rsplit("/", 1)[-1]


def test_public_registry_defaults_to_ghcr(monkeypatch) -> None:
    monkeypatch.delenv("NPA_PUBLIC_REGISTRY", raising=False)
    assert public_container_registry() == DEFAULT_PUBLIC_CONTAINER_REGISTRY
    assert DEFAULT_PUBLIC_CONTAINER_REGISTRY.startswith("ghcr.io/")


def test_public_registry_honors_env_override(monkeypatch) -> None:
    monkeypatch.setenv("NPA_PUBLIC_REGISTRY", "docker.io/nebius/workbench")
    assert public_container_registry() == "docker.io/nebius/workbench"


def test_publish_plan_targets_public_registry_by_default() -> None:
    plan = build_publish_plan(target_registry=DEFAULT_PUBLIC_CONTAINER_REGISTRY)
    assert len(plan) == len(CONTAINER_IMAGE_NAMES) - len(OMNIVERSE_RESTRICTED_TOOLS) == 15
    for item in plan:
        assert item.target_ref.startswith(DEFAULT_PUBLIC_CONTAINER_REGISTRY + "/npa-")


def test_selector_matches_packaging_contract_classification() -> None:
    """The selector and the packaging contract must agree in both directions, so
    reclassifying an image in one place cannot silently widen or narrow what the
    publisher ships."""
    images = _contract()["images"]
    for image_name, entry in images.items():
        tool = CONTRACT_ALIASES.get(image_name, image_name)
        if entry["redistribution"] == "restricted":
            if tool in CONTAINER_IMAGE_NAMES:
                assert not is_publicly_redistributable(tool), image_name
            else:
                # A non-canonical restricted image must still map onto a
                # restricted canonical tool (e.g. sonic-mujoco -> sonic).
                assert tool in OMNIVERSE_RESTRICTED_TOOLS, image_name
        elif tool in CONTAINER_IMAGE_NAMES:
            assert is_publicly_redistributable(tool), image_name
