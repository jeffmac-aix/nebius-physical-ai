"""Antioch wrapper around the shared NPA Isaac/OpenPI bridge implementation."""

from __future__ import annotations

import antioch

from reverse_policy_relay import ReversePolicyRelay


@antioch.scenario(tags=["npa-openpi-franka"])
def openpi_franka_camera_bridge(run: antioch.ScenarioRun) -> None:
    """Use Antioch-owned Kit startup and the same bridge exercised on Kubernetes."""

    from npa.workbench.antioch.openpi_isaac import run as run_bridge

    # Antioch's authenticated port tunnel is local -> assigned machine.  The
    # reverse relay lets a local connector carry the private Kubernetes policy
    # stream back through that tunnel without exposing the policy or copying a
    # Kubernetes credential into the hosted simulator.
    with ReversePolicyRelay(backend_port=18123, frontend_port=8000):
        report = run_bridge(launch_application=False)
    run.add_result("policy_action_shape", report["policy_action_shape"])
    run.add_result("targets_executed", report["targets_executed"])
    run.check(
        "OpenPI returned an exact finite 15x8 target chunk",
        report["policy_action_shape"] == [15, 8],
    )
    run.check(
        "the fail-closed bridge safely applied position targets",
        int(report["targets_executed"]) > 0 and report["fail_closed"] is True,
    )
