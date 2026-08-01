"""Mirror the OSS-redistributable workbench images to a public registry.

Nebius Container Registry does not support anonymous/public pulls, so "public
exposure" of the workbench means mirroring the publicly-redistributable image
subset to a public-capable registry (e.g. GHCR ``ghcr.io/<org>/<repo>``).

This tool is license-guarded: it only ever copies tools reported by
``images.publicly_publishable_tools()`` and hard-refuses anything in
``images.OMNIVERSE_RESTRICTED_TOOLS`` as defence in depth around that selector.

That set is currently empty. It used to hold ``isaac-lab``, ``sonic`` and ``groot``
(plus the derived ``sonic-mujoco``), which baked NVIDIA Omniverse Kit; those images
were re-architected to fetch Isaac Sim / Isaac Lab at first run under the operator's
own EULA acceptance, so all 19 workbench images are now publishable. The refusal is
kept, and tested against a synthetic restricted tool, for the next runtime we cannot
ship.

Example (dry run first, then execute):

    python -m npa.deploy.publish_public --target ghcr.io/nebius/nebius-physical-ai --dry-run
    python -m npa.deploy.publish_public --target ghcr.io/nebius/nebius-physical-ai
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

from npa.deploy import images
from npa.deploy.images import (
    container_image_for_tool,
    is_publicly_redistributable,
    omniverse_restricted_image_names,
    primary_container_registry,
    public_container_registry,
    publicly_publishable_tools,
)


@dataclass(frozen=True)
class PublishItem:
    tool: str
    source_ref: str
    target_ref: str


def build_publish_plan(
    *,
    target_registry: str,
    source_registry: str | None = None,
) -> list[PublishItem]:
    """Return the (source -> target) copy plan for the public image subset.

    Raises ``ValueError`` if an Omniverse-restricted tool ever leaks into the
    plan (defense in depth around the license boundary).
    """
    if not target_registry.strip():
        raise ValueError("target_registry is required")
    source_registry = source_registry or primary_container_registry()
    target = target_registry.rstrip("/")

    plan: list[PublishItem] = []
    for tool in publicly_publishable_tools():
        # Read the restricted set through the module, never a from-import: a
        # defence-in-depth check that holds a stale copy of the thing it is defending is
        # worse than no check at all. (`from ... import OMNIVERSE_RESTRICTED_TOOLS` binds
        # the value at import time, so this guard and publicly_publishable_tools() could
        # disagree - which is exactly what a test caught once the set stopped being empty.)
        if not is_publicly_redistributable(tool) or tool in images.OMNIVERSE_RESTRICTED_TOOLS:
            raise ValueError(
                f"refusing to publish restricted (Omniverse Kit) tool {tool!r} to a public registry"
            )
        source_ref = container_image_for_tool(tool, registry=source_registry)
        image = source_ref.rsplit("/", 1)[-1]  # npa-<tool>:<tag>
        plan.append(PublishItem(tool=tool, source_ref=source_ref, target_ref=f"{target}/{image}"))
    return plan


# --------------------------------------------------------------------------------------
# Anonymous pullability
#
# Pushing to GHCR is NOT the same as publishing. A newly created container package is
# PRIVATE, and a package linked to a repository inherits that repository's access
# *permissions* but explicitly NOT its visibility -- so even a public repo yields private
# packages. Worse, GitHub exposes no REST API to change visibility for ORGANISATION-owned
# packages: it is a manual step in the package's settings UI, and it is one-way (a public
# package cannot be made private again).
#
# Without the check below, `publish_public` copies 19 images, exits 0, and reports success
# while nothing is actually publicly pullable -- a silent false success on the one action
# in this repo that cannot be undone. So the publish path verifies the outcome it claims.
# --------------------------------------------------------------------------------------

_ANON_TIMEOUT_SECONDS = 30


def _registry_host(ref: str) -> str:
    return ref.split("/", 1)[0]


def anonymous_pull_ok(ref: str, *, timeout: float = _ANON_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """Whether ``ref`` can be pulled with NO credentials at all.

    This is the property that actually matters to an external consumer, and the only one
    that distinguishes "pushed" from "published". Implemented with plain HTTP rather than
    a docker/crane call so it cannot accidentally reuse an ambient login and report a
    private package as public -- the whole point is to check the unauthenticated path.
    """
    host = _registry_host(ref)
    remainder = ref[len(host) + 1 :]
    repository, _, reference = remainder.rpartition(":")
    if not repository:  # digest-style or malformed
        repository, reference = remainder, "latest"

    token = ""
    if host == "ghcr.io":
        # GHCR hands an anonymous bearer token to anyone; it simply carries no rights for
        # a private package, so the manifest request is what actually decides.
        try:
            url = f"https://ghcr.io/token?scope=repository:{repository}:pull&service=ghcr.io"
            with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
                token = json.loads(response.read()).get("token", "")
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            return False, f"could not obtain an anonymous token: {exc}"

    request = urllib.request.Request(  # noqa: S310 - https registry API
        f"https://{host}/v2/{repository}/manifests/{reference}",
        method="GET",
        headers={
            "Accept": (
                "application/vnd.oci.image.index.v1+json,"
                "application/vnd.oci.image.manifest.v1+json,"
                "application/vnd.docker.distribution.manifest.list.v2+json,"
                "application/vnd.docker.distribution.manifest.v2+json"
            ),
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status == 200, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code in (401, 403):
            hint = " (package is private — set its visibility to Public in the package settings)"
        return False, f"HTTP {exc.code}{hint}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, f"unreachable: {exc}"


def verify_public(plan: list[PublishItem]) -> list[tuple[PublishItem, str]]:
    """Return the plan items that are NOT anonymously pullable, with the reason."""
    failures: list[tuple[PublishItem, str]] = []
    for item in plan:
        ok, detail = anonymous_pull_ok(item.target_ref)
        status = "public" if ok else f"NOT PUBLIC — {detail}"
        print(f"  {item.target_ref}  {status}")
        if not ok:
            failures.append((item, detail))
    return failures


def _crane_copy(item: PublishItem) -> None:
    crane = shutil.which("crane")
    if not crane:
        raise RuntimeError("crane not found on PATH; install go-containerregistry crane")
    subprocess.run([crane, "copy", item.source_ref, item.target_ref], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        default=public_container_registry(),
        help="Target public registry (e.g. ghcr.io/nebius/nebius-physical-ai); "
        "defaults to $NPA_PUBLIC_REGISTRY.",
    )
    parser.add_argument(
        "--source-registry",
        default=None,
        help="Source registry to copy from (defaults to the primary Nebius registry).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without copying.")
    parser.add_argument(
        "--verify-public",
        action="store_true",
        help=(
            "Do not copy. Check that every planned target is pullable with NO credentials, "
            "and exit non-zero if any is not. Pushing to GHCR leaves packages PRIVATE, and "
            "there is no API to change that for org-owned packages, so this is how a "
            "publish proves it actually published."
        ),
    )
    args = parser.parse_args(argv)

    if not (args.target or "").strip():
        parser.error("no target registry; pass --target or set NPA_PUBLIC_REGISTRY")

    plan = build_publish_plan(target_registry=args.target, source_registry=args.source_registry)
    restricted = omniverse_restricted_image_names()
    print(f"Publishing {len(plan)} OSS image(s) to {args.target.rstrip('/')}")
    if restricted:
        print(
            "Excluded (bakes a runtime we may not redistribute): "
            + ", ".join(restricted)
        )
    else:
        # Don't print a dangling "Excluded: " with nothing after it, and say why the
        # list is empty — an operator reading this needs to know that the Isaac images
        # being absent from the exclusion list is intended, not an oversight.
        print(
            "Excluded: none — every workbench image is publicly redistributable. The "
            "Isaac images fetch Isaac Sim / Isaac Lab at first run under the operator's "
            "own EULA acceptance rather than baking it."
        )
    for item in plan:
        print(f"  {item.source_ref}  ->  {item.target_ref}")
    if args.verify_public:
        print("\nVerifying anonymous (unauthenticated) pullability:")
        failures = verify_public(plan)
        if failures:
            print(
                f"\n{len(failures)} of {len(plan)} image(s) are NOT publicly pullable.\n"
                "Pushing to GHCR does not publish: a new container package is private, and a\n"
                "package linked to a repository inherits the repository's access permissions\n"
                "but NOT its visibility. GitHub offers no REST API to change visibility for\n"
                "organisation-owned packages, so this is a one-time MANUAL step per package:\n"
                "  https://github.com/orgs/<org>/packages -> <package> -> Package settings\n"
                "  -> Danger Zone -> Change visibility -> Public\n"
                "Note it is irreversible: a public package cannot be made private again.",
                file=sys.stderr,
            )
            return 1
        print(f"\nAll {len(plan)} image(s) are publicly pullable.")
        return 0

    if args.dry_run:
        print("(dry run — nothing copied)")
        return 0
    for item in plan:
        _crane_copy(item)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
