"""License-guarded public-registry publishing.

Nebius CR has no anonymous/public mode, so public exposure means mirroring the
OSS-redistributable image subset to a public registry. These tests lock the license
boundary: whatever is classified non-redistributable must never be selected for a public
registry, and the selector must stay in sync with the packaging contract's
``redistribution:`` fields.

``OMNIVERSE_RESTRICTED_TOOLS`` is currently EMPTY — the four Isaac images were
re-architected to fetch Isaac Sim / Isaac Lab at first run under the operator's own EULA
acceptance instead of baking it, so every workbench tool is now publishable. That makes
the boundary tests the delicate ones: asserting "nothing is restricted" would pass just
as well against a guard that had been deleted. So the tests that exercise the refusal
monkeypatch a synthetic restricted tool in, proving the mechanism still bites while its
membership is empty.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from npa.deploy import images
from npa.deploy.images import (
    CONTAINER_IMAGE_NAMES,
    DEFAULT_PUBLIC_CONTAINER_REGISTRY,
    OMNIVERSE_RESTRICTED_DERIVED_IMAGES,
    OMNIVERSE_RESTRICTED_TOOLS,
    container_image_for_tool,
    is_public_registry,
    is_publicly_redistributable,
    omniverse_restricted_image_names,
    public_container_registry,
    publicly_publishable_tools,
)
from npa.deploy.publish_public import build_publish_plan

ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = ROOT / "npa" / "docker" / "workbench" / "packaging-contract.yaml"


def test_isaac_images_are_no_longer_restricted() -> None:
    """Removing baked Omniverse Kit made the Isaac images publishable.

    They now fetch Isaac Sim / Isaac Lab at first run from pypi.nvidia.com under the
    operator's own EULA acceptance and ship no NVIDIA Isaac bytes, verified against the
    built image by npa/scripts/scan_image_omniverse_payload.py (isaac-lab: 83,043 entries
    scanned; sonic: 125,655 entries; both VERDICT clean).
    """
    for tool in ("isaac-lab", "sonic", "groot"):
        assert is_publicly_redistributable(tool), tool


def test_no_tool_is_currently_restricted() -> None:
    """Nothing is excluded any more, which took THREE separate fixes for sonic.

    Omniverse Kit was only the first: sonic also baked gated model weights (git-LFS
    smudging) and NVIDIA Omniverse 3D assets (the RoboCasa asset library under
    decoupled_wbc/dexmg). Both were found by scanning the built image, and neither was
    visible in the Dockerfile. The scan that clears it:
    npa-sonic:0.1.2-rtfetch-rc5, 125,655 entries, 16 allowlisted paths, VERDICT clean.
    """
    assert OMNIVERSE_RESTRICTED_TOOLS == frozenset()
    assert OMNIVERSE_RESTRICTED_DERIVED_IMAGES == frozenset()
    for tool in ("isaac-lab", "sonic", "groot"):
        assert is_publicly_redistributable(tool), tool


def test_public_set_excludes_every_restricted_tool(monkeypatch) -> None:
    """The exclusion still works. Monkeypatched, because the real set is empty and an
    all-inclusive selector would satisfy an assertion over an empty set trivially."""
    monkeypatch.setattr(images, "OMNIVERSE_RESTRICTED_TOOLS", frozenset({"genesis", "cosmos"}))
    public = set(publicly_publishable_tools())
    assert public.isdisjoint({"genesis", "cosmos"})
    for tool in ("genesis", "cosmos"):
        assert not is_publicly_redistributable(tool)
    assert "lerobot" in public, "unrelated tools must stay publishable"


def test_public_set_includes_the_oss_tools() -> None:
    public = set(publicly_publishable_tools())
    for tool in (
        "lerobot",
        "genesis",
        "cosmos",
        "fiftyone",
        "lancedb",
        "rerun-viewer",
        "lichtblick",
        # Newly publishable: no baked Omniverse Kit, weights or assets.
        "isaac-lab",
        "sonic",
        "groot",
    ):
        assert tool in public, tool
    assert public == set(CONTAINER_IMAGE_NAMES) - OMNIVERSE_RESTRICTED_TOOLS


def test_publish_plan_now_includes_the_isaac_images() -> None:
    """The point of the re-architecture: these are publishable at last."""
    plan = build_publish_plan(target_registry="ghcr.io/example/workbench")
    names = {item.source_ref.rsplit("/", 1)[-1].split(":", 1)[0] for item in plan}
    for image in ("npa-isaac-lab", "npa-sonic", "npa-groot"):
        assert image in names, image
    # sonic-mujoco is a sonic variant, so it ships through sonic's image manifest rather
    # than as its own tool key.
    assert "npa-sonic-mujoco" not in names
    for item in plan:
        assert item.target_ref.startswith("ghcr.io/example/workbench/")


def test_publish_plan_still_refuses_a_restricted_image(monkeypatch) -> None:
    """The hard refusal inside build_publish_plan is defence in depth around the selector.

    Monkeypatching the set is also what pins that the refusal reads it through the module
    rather than through a from-import: a stale binding made this guard disagree with
    publicly_publishable_tools(), so a tool the selector considered publishable tripped the
    refusal and the whole plan raised. A defence-in-depth check holding a stale copy of the
    thing it defends is worse than no check.
    """
    monkeypatch.setattr(images, "OMNIVERSE_RESTRICTED_TOOLS", frozenset({"genesis"}))
    plan = build_publish_plan(target_registry="ghcr.io/example/workbench")
    names = {item.source_ref.rsplit("/", 1)[-1].split(":", 1)[0] for item in plan}
    assert "npa-genesis" not in names
    # sonic is publishable under this monkeypatched set, so the plan must contain it -
    # proving the refusal followed the patched set instead of a captured one.
    assert "npa-sonic" in names


def test_publish_plan_requires_a_target() -> None:
    with pytest.raises(ValueError):
        build_publish_plan(target_registry="")


def test_publish_plan_copies_the_pinned_tag_unchanged() -> None:
    """A mirror must serve the same ``name:tag`` the primary registry serves, or
    every pin in the repo (and every customer's) breaks against the mirror."""
    plan = build_publish_plan(
        target_registry="ghcr.io/example/workbench",
        source_registry="cr.eu-north1.nebius.cloud/example",
    )
    assert plan
    for item in plan:
        source_image = item.source_ref.rsplit("/", 1)[-1]
        target_image = item.target_ref.rsplit("/", 1)[-1]
        assert source_image == target_image, item


def test_public_registry_defaults_to_ghcr(monkeypatch) -> None:
    monkeypatch.delenv("NPA_PUBLIC_REGISTRY", raising=False)
    assert public_container_registry() == DEFAULT_PUBLIC_CONTAINER_REGISTRY
    assert DEFAULT_PUBLIC_CONTAINER_REGISTRY.startswith("ghcr.io/")


def test_public_registry_honors_env_override(monkeypatch) -> None:
    monkeypatch.setenv("NPA_PUBLIC_REGISTRY", "docker.io/nebius/workbench")
    assert public_container_registry() == "docker.io/nebius/workbench"


def test_publish_plan_targets_public_registry_by_default() -> None:
    plan = build_publish_plan(target_registry=DEFAULT_PUBLIC_CONTAINER_REGISTRY)
    # Was 16 (every tool except isaac-lab, sonic and groot). Derived rather than
    # hardcoded so adding a tool cannot silently leave it unpublished.
    # Was 16 (isaac-lab, sonic and groot all excluded). Derived rather than hardcoded so
    # adding a tool cannot silently leave it unpublished.
    assert len(plan) == len(CONTAINER_IMAGE_NAMES) - len(OMNIVERSE_RESTRICTED_TOOLS) == 19
    for item in plan:
        assert item.target_ref.startswith(DEFAULT_PUBLIC_CONTAINER_REGISTRY + "/npa-")


def test_restricted_image_names_cover_every_contract_restricted_image() -> None:
    """The operator-facing excluded list must name every restricted image, derived
    variants included, without any caller hardcoding them.

    Both sides are currently empty, which is the property being locked: the code and the
    packaging contract must agree about what may not be published.
    """
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    contract_restricted = {
        name
        for name, entry in contract["images"].items()
        if entry.get("redistribution") == "restricted"
    }
    names = omniverse_restricted_image_names()
    assert names == sorted(names), "names must be stable/sorted for operator output"
    assert contract_restricted <= set(names), sorted(contract_restricted - set(names))
    assert set(OMNIVERSE_RESTRICTED_DERIVED_IMAGES).isdisjoint(CONTAINER_IMAGE_NAMES)
    assert set(OMNIVERSE_RESTRICTED_DERIVED_IMAGES).isdisjoint(publicly_publishable_tools())


def test_contract_marks_the_isaac_images_public_and_runtime_fetch() -> None:
    """The contract must record BOTH facts: publishable, and what earns it.

    `redistribution: public` on its own would look like someone relabelled four restricted
    images; `isaac_runtime_fetch: true` is the claim that earns it, and
    npa/tests/docker/test_packaging_contract.py checks the Dockerfiles implement it.
    """
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    for name in ("isaac-lab", "sonic", "sonic-mujoco", "groot"):
        entry = contract["images"][name]
        assert entry["redistribution"] == "public", name
        assert entry.get("isaac_runtime_fetch") is True, name


def test_the_restriction_mechanism_still_exists() -> None:
    """Deliberately kept with an empty membership, not deleted.

    The next runtime we cannot ship needs exactly this machinery, and a mechanism that
    gets deleted when unused has to be rebuilt and re-reviewed under time pressure.
    """
    assert hasattr(images, "OMNIVERSE_RESTRICTED_TOOLS")
    assert hasattr(images, "OMNIVERSE_RESTRICTED_DERIVED_IMAGES")
    assert omniverse_restricted_image_names() == []
    for symbol in (
        "is_publicly_redistributable",
        "omniverse_restricted_image_names",
        "publicly_publishable_tools",
        "is_public_registry",
    ):
        assert callable(getattr(images, symbol)), symbol
    assert "restricted" in yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))[
        "redistribution"
    ]["classes"], "the restricted class must survive having no members"


def test_selector_matches_packaging_contract_classification() -> None:
    """Every image the packaging contract marks ``restricted`` must resolve to a
    tool that the selector also treats as non-public (kept in sync).

    Vacuous while nothing is restricted; kept so that classifying something restricted
    again immediately re-arms the sync check.
    """
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    # contract image keys that map onto canonical tool keys
    for image_name, entry in contract["images"].items():
        if entry.get("redistribution") != "restricted":
            continue
        # sonic-mujoco is a sonic variant (covered by the "sonic" restriction)
        tool = "sonic" if image_name == "sonic-mujoco" else image_name
        if tool in CONTAINER_IMAGE_NAMES:
            assert not is_publicly_redistributable(tool), image_name
        else:
            # non-canonical restricted image (e.g. sonic-mujoco) must map to a
            # restricted canonical tool
            assert tool in OMNIVERSE_RESTRICTED_TOOLS, image_name


# --- Resolution guard: a restricted tool must never resolve from a public registry ----
#
# The docs tell external consumers to point NPA_REGISTRY at the public mirror. Asking
# for a restricted tool in that state used to silently produce a public image reference
# for something we must never publish. Private registries are unaffected —
# build-your-own is the licensed path, whichever registry that is.


@pytest.mark.parametrize(
    "registry",
    [
        "ghcr.io/nebius/nebius-physical-ai",
        "docker.io/nebius/workbench",
        "quay.io/nebius/workbench",
        "public.ecr.aws/nebius/workbench",
    ],
)
def test_restricted_tools_refuse_to_resolve_from_a_public_registry(
    monkeypatch, registry
) -> None:
    monkeypatch.setattr(images, "OMNIVERSE_RESTRICTED_TOOLS", frozenset({"genesis"}))
    with pytest.raises(ValueError, match="not publicly redistributable"):
        container_image_for_tool("genesis", registry=registry)


def test_restricted_tools_still_resolve_from_an_operators_own_registry(monkeypatch) -> None:
    """Build-your-own into a private registry is the licensed path; do not block it."""
    monkeypatch.setattr(images, "OMNIVERSE_RESTRICTED_TOOLS", frozenset({"genesis"}))
    ref = container_image_for_tool("genesis", registry="cr.eu-north1.nebius.cloud/example")
    assert ref.startswith("cr.eu-north1.nebius.cloud/example/npa-genesis:")


def test_public_registry_detection() -> None:
    assert is_public_registry("ghcr.io/nebius/nebius-physical-ai")
    assert is_public_registry("GHCR.IO/Nebius/Workbench")
    assert not is_public_registry("cr.eu-north1.nebius.cloud/e00example")
    assert not is_public_registry("")


def test_public_mirror_override_is_treated_as_public(monkeypatch) -> None:
    """Whatever is configured as the mirror is public, even on a private-looking host."""
    monkeypatch.setenv("NPA_PUBLIC_REGISTRY", "mirror.example.com/workbench")
    assert is_public_registry("mirror.example.com/workbench")


def test_oss_tools_resolve_from_the_public_mirror_normally() -> None:
    """The guard must not get in the way of the images that ARE publishable."""
    ref = container_image_for_tool("lerobot", registry=DEFAULT_PUBLIC_CONTAINER_REGISTRY)
    assert ref.startswith(DEFAULT_PUBLIC_CONTAINER_REGISTRY + "/npa-lerobot:")
