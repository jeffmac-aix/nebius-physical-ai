"""Workbench images must stay schedulable by SkyPilot on Kubernetes.

SkyPilot's Kubernetes runtime bootstrap runs inside the task container and needs a
system ``python3`` (plus ``rsync``). A vendor image that ships only its own interpreter
cannot host a task at all: provisioning fails with

    KubernetesError: Failed to get ssh user for pod ...: container not found ("ray-node")

which is what the Isaac Lab image did on npa-rtxpro-mk8s until the prerequisites were
added. These are cheap textual guards so the requirement cannot silently regress, and
so a reviewer can see *why* the lines exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

DOCKER_ROOT = Path(__file__).resolve().parents[2] / "docker" / "workbench"

# Images whose stages are submitted through SkyPilot (npa.workflow / workbench
# workflows) and therefore must be schedulable in a pod. This list grows as the raw
# SkyPilot task catalog is retired: once a tool's only workflow surface is an
# npa.workflow spec, its image MUST be able to host a SkyPilot task.
SKYPILOT_HOSTED_IMAGES = ("cosmos3-reason", "isaac-lab", "lerobot", "sonic")

#: Images built on an Isaac base, where /isaac-sim is mode 750 owned by
#: isaac-sim:isaac-sim and the runtime user therefore has to join that GROUP (a
#: recursive chmod would rewrite multi-GB layers). Not universal: the lerobot image has
#: no /isaac-sim at all, so requiring the usermod there would pin a no-op.
ISAAC_BASED_IMAGES = ("isaac-lab", "sonic")


#: The four ingredients a SkyPilot-hosted image needs, established by bisecting
#: derived images against a live Kubernetes GPU cluster. Missing any one of them makes
#: provisioning fail with `container not found ("ray-node")`.
REQUIRED_INGREDIENTS = (
    ("python3", "SkyPilot's k8s runtime bootstrap needs a system python3"),
    ("rsync", "SkyPilot syncs files with rsync"),
    ("NOPASSWD", "SkyPilot's in-pod setup shells out to sudo without a password"),
    ("ENV PATH=/usr/bin:$PATH", "the system interpreter must precede a vendor python"),
)


@pytest.mark.parametrize("tool", SKYPILOT_HOSTED_IMAGES)
@pytest.mark.parametrize(("token", "why"), REQUIRED_INGREDIENTS)
def test_dockerfile_has_skypilot_runtime_prerequisites(tool: str, token: str, why: str) -> None:
    dockerfile = DOCKER_ROOT / tool / "Dockerfile"
    assert dockerfile.is_file(), dockerfile
    text = dockerfile.read_text(encoding="utf-8")
    assert token in text, f"{tool}: {why}"


@pytest.mark.parametrize("tool", SKYPILOT_HOSTED_IMAGES)
def test_skypilot_hosted_image_stays_non_root(tool: str) -> None:
    """The prerequisites make a NON-root image schedulable; keep it that way.

    An image that simply ends as root also works, but that is a needless privilege
    escalation for every stage the workbench runs.
    """

    text = (DOCKER_ROOT / tool / "Dockerfile").read_text(encoding="utf-8")
    user_lines = [
        line.strip() for line in text.splitlines() if line.strip().startswith("USER ")
    ]
    assert user_lines and user_lines[-1] != "USER root", (
        f"{tool}: image must not end as root; the sudo/group/PATH ingredients exist "
        "precisely so a non-root image can host a SkyPilot task"
    )


def test_isaac_lab_grants_its_runtime_user_access_to_isaac_sim() -> None:
    """/isaac-sim is 750 isaac-sim:isaac-sim, so the pod user needs the group.

    Group membership (not a recursive chown/chmod) keeps the fix to a tiny layer
    instead of rewriting multi-GB Isaac layers.
    """

    text = (DOCKER_ROOT / "isaac-lab" / "Dockerfile").read_text(encoding="utf-8")
    assert "usermod -aG isaac-sim ubuntu" in text
    # Check instructions only: the rationale comment names the approach it avoids.
    instructions = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "chmod -R" not in instructions, (
        "a recursive chmod would rewrite multi-GB Isaac layers; use group membership"
    )


@pytest.mark.parametrize("tool", SKYPILOT_HOSTED_IMAGES)
def test_derived_prereq_dockerfile_matches_the_shipped_one(tool: str) -> None:
    """The derived recipe exists and applies the same prerequisites.

    Operators use it to repair an already-published tag without pulling the multi-GB
    base (scripts/build-workbench-image-in-cluster.sh); it must not drift from the
    image.
    """

    derived = DOCKER_ROOT / tool / "Dockerfile.k8s-prereqs"
    assert derived.is_file(), derived
    text = derived.read_text(encoding="utf-8")
    for token in (
        "python3",
        "rsync",
        "NOPASSWD",
        "ENV PATH=/usr/bin:$PATH",
        "ARG BASE_IMAGE",
    ):
        assert token in text, f"{tool}: derived prereq Dockerfile is missing {token!r}"
    if tool in ISAAC_BASED_IMAGES:
        assert "usermod -aG isaac-sim" in text, (
            f"{tool} derives from an Isaac base, where /isaac-sim is mode 750 "
            "isaac-sim:isaac-sim, so the runtime user must join that group"
        )
        # Scheduling is not enough: Kit also has to be able to WRITE. Without these three
        # directories Isaac boots, fails to save its user config, and then renders nothing
        # while burning CPU — live job 271 stalled for 45 minutes that way, which is far
        # harder to diagnose than a pod that never starts.
        for kit_dir in ("/isaac-sim/kit/data", "/isaac-sim/kit/logs", "/isaac-sim/kit/cache"):
            assert kit_dir in text, (
                f"{tool}: {kit_dir} must exist and belong to the runtime user, or Kit stalls"
            )
        assert "chown -R ubuntu:ubuntu /isaac-sim/kit/data" in text


def test_in_cluster_build_script_is_executable_and_generic() -> None:
    script = Path(__file__).resolve().parents[3] / "scripts" / "build-workbench-image-in-cluster.sh"
    assert script.is_file(), script
    text = script.read_text(encoding="utf-8")
    # No hardcoded registry/bucket/project identifiers.
    assert "cr.us-central1" not in text and "cr.eu-north1" not in text
    for flag in ("--base", "--tag", "--dockerfile", "--pull-secret", "--namespace"):
        assert flag in text, f"build script should accept {flag}"
