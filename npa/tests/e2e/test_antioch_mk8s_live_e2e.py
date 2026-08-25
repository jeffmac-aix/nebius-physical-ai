"""Sustained live qualification for the MK8s-native Antioch/OpenPI path.

This test intentionally retains the accepted Antioch simulator, adapter pod,
and policy Deployment. Cleanup is an explicit operator action after viewing.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from npa.sdk.workbench.antioch import live_k8s_deploy, live_k8s_status

pytestmark = pytest.mark.e2e_pipeline


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


if not _enabled("NPA_INTEGRATION_E2E"):
    pytest.skip(
        "set NPA_INTEGRATION_E2E=1 for live infrastructure",
        allow_module_level=True,
    )

if os.environ.get("NPA_ANTIOCH_ACCEPT_TERMS") != "YES":
    pytest.skip("operator Antioch terms acceptance is absent", allow_module_level=True)
if os.environ.get("ACCEPT_EULA") != "Y":
    pytest.skip("operator NVIDIA runtime acceptance is absent", allow_module_level=True)
if os.environ.get("NPA_OPENPI_ACCEPT_GEMMA_TERMS") != "YES":
    pytest.skip("operator Gemma terms acceptance is absent", allow_module_level=True)

_RUNTIME_CONFIG_VALUE = os.environ.get("NPA_ANTIOCH_MK8S_RUNTIME_CONFIG", "").strip()
if not _RUNTIME_CONFIG_VALUE:
    pytest.skip(
        "set NPA_ANTIOCH_MK8S_RUNTIME_CONFIG to a mode-0600 private config",
        allow_module_level=True,
    )
RUNTIME_CONFIG = Path(_RUNTIME_CONFIG_VALUE)


def _accepted(metrics: dict[str, int | float]) -> bool:
    requests = int(metrics.get("requests", 0))
    round_trips = int(metrics.get("round_trips", 0))
    success_rate = round_trips / max(requests, 1)
    rejection_keys = (
        "rejected_wrong_shape",
        "rejected_non_finite",
        "rejected_joint_limit",
        "rejected_gripper_range",
        "rejected_joint_step",
    )
    return (
        float(metrics.get("elapsed_seconds", 0)) >= 120
        and int(metrics.get("frames", 0)) >= 120
        and round_trips >= 100
        and int(metrics.get("applied", 0)) >= 500
        and success_rate >= 0.90
        and all(int(metrics.get(key, 0)) == 0 for key in rejection_keys)
        and float(metrics.get("luminance_mean_min", 0)) > 5
        and float(metrics.get("luminance_variance_min", 0)) > 25
        and float(metrics.get("latency_p95_ms", float("inf"))) <= 2_000
        and float(metrics.get("latency_p99_ms", float("inf"))) <= 90_000
        and float(metrics.get("latency_max_ms", float("inf"))) <= 90_000
        and int(metrics.get("reconnects", 0)) <= 5
    )


def test_real_franka_camera_policy_loop_sustains_cluster_native_acceptance() -> None:
    deployed = live_k8s_deploy(runtime_config=RUNTIME_CONFIG)
    assert deployed["policy_service_type"] == "ClusterIP"
    assert deployed["dev_vm_in_data_path"] is False
    while True:
        status = live_k8s_status(runtime_config=RUNTIME_CONFIG)
        metrics = status.get("live_metrics") or {}
        if _accepted(metrics):
            assert status["status"] == "ready"
            assert status["adapter_restarts"] == 0
            assert status["cluster_local_policy_resolved"] is True
            assert status["dev_vm_in_data_path"] is False
            return
        time.sleep(5)
