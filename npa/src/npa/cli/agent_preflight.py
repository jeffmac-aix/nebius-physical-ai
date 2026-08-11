"""Cheap, side-effect-free NPA agent deployment preflight checks."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from npa.clients.credentials import load_credentials
from npa.workflows.sim2real_health import (
    CheckResult,
    FAIL,
    PASS,
    WARN,
    format_check_report,
    has_failure,
)


def agent_hard_prereq_results(
    ssh_public_key_path: str, *, terraform_bin: str | None = None
) -> list[CheckResult]:
    """Check Terraform and the SSH keypair before any cloud-side mutation."""
    results: list[CheckResult] = []
    terraform = (
        terraform_bin
        if terraform_bin is not None
        else os.environ.get("NPA_TERRAFORM_BIN") or shutil.which("terraform") or ""
    ).strip()
    if terraform:
        results.append(CheckResult("terraform", PASS, f"terraform found ({terraform})."))
    else:
        results.append(
            CheckResult(
                "terraform",
                FAIL,
                "terraform binary not found on PATH.",
                remedy="Install it: https://developer.hashicorp.com/terraform/install",
            )
        )

    public_path = Path(ssh_public_key_path).expanduser()
    private_path = Path(str(public_path)[:-4] if str(public_path).endswith(".pub") else str(public_path))
    if public_path.is_file():
        results.append(CheckResult("ssh_public_key", PASS, f"SSH public key present ({public_path})."))
    else:
        results.append(
            CheckResult(
                "ssh_public_key",
                FAIL,
                f"SSH public key not found: {public_path}",
                remedy=(
                    f"Generate a keypair (`ssh-keygen -t ed25519 -f {private_path}`) "
                    "or pass --ssh-public-key-path to an existing key."
                ),
            )
        )
    if private_path.is_file():
        results.append(CheckResult("ssh_private_key", PASS, f"SSH private key present ({private_path})."))
    else:
        results.append(
            CheckResult(
                "ssh_private_key",
                FAIL,
                f"SSH private key not found: {private_path}",
                remedy="The private key next to the public key is required to bootstrap the VM over SSH.",
            )
        )
    return results


def agent_token_factory_result(tf_key: str | None = None) -> CheckResult:
    """Report Token Factory availability without making a network request."""
    if tf_key is None:
        tf_key = load_credentials().token_factory_api_key
    if tf_key:
        return CheckResult("token_factory", PASS, "Token Factory API key is configured.")
    return CheckResult(
        "token_factory",
        WARN,
        "Token Factory API key not found; agent chat will return 503 until it is set.",
        remedy=(
            "Get a key (starts with 'v1.') at https://tokenfactory.nebius.com/ and run "
            "`npa configure --token-factory-key <key>`, then re-run `npa agent bootstrap`."
        ),
    )


def agent_nebius_auth_result() -> CheckResult:
    """Require a live Nebius CLI identity before deployment."""
    try:
        from npa.clients.nebius import get_iam_token

        token = get_iam_token()
    except Exception as exc:  # noqa: BLE001 - every auth/CLI error means not ready
        return CheckResult(
            "nebius_profile",
            FAIL,
            "No authenticated Nebius CLI profile.",
            remedy="Install/authenticate the Nebius CLI and run `npa configure`.",
            details=(str(exc),),
        )
    if token:
        return CheckResult("nebius_profile", PASS, "Nebius CLI profile is authenticated.")
    return CheckResult(
        "nebius_profile",
        FAIL,
        "Nebius IAM token unavailable.",
        remedy="Run `npa configure` / `nebius profile create` to authenticate.",
    )


def render_agent_checks(results: list[CheckResult], *, output_json: bool) -> tuple[str, bool]:
    """Return the shared rendered report and whether it contains a failure."""
    return format_check_report(results, output_json=output_json), has_failure(results)
