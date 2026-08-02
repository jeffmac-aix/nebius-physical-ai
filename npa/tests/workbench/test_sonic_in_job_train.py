"""`--runtime in-job`: train where the stage already is.

The SONIC train toolRef used `--runtime serverless`, which provisions a Nebius Job from inside
the pod. A workflow stage cannot do that — it failed live with
`SONIC --runtime serverless requires --project-id` (EVIDENCE §R11) — and even with a project id
it would mean a workflow launching infrastructure the workflow engine has already provisioned.
"""

from __future__ import annotations

import shlex

from npa.cli.workbench.sonic.train import (
    build_sonic_in_job_train_script,
    build_sonic_serverless_train_command,
    build_sonic_train_body,
)


def _kwargs(**overrides):
    kwargs = dict(
        checkpoint="nvidia/sonic-base",
        data_path="s3://bucket/motions/",
        sample_data=False,
        embodiment="g1",
        num_envs=64,
        headless=True,
        max_iterations=5,
        isaac_lab_version="2.3.2",
    )
    kwargs.update(overrides)
    return kwargs


def test_both_runtimes_run_the_same_training_body() -> None:
    """Two runtimes with two training scripts would be two trainers."""

    body = build_sonic_train_body(**_kwargs())
    remote = build_sonic_serverless_train_command(**_kwargs())

    # The serverless command is exactly the body, wrapped for remote execution.
    assert remote == f"bash -lc {shlex.quote(body)}"
    assert "/entrypoint.sh train" in body


def test_the_in_job_script_publishes_where_it_was_told() -> None:
    script = build_sonic_in_job_train_script(
        output_path="s3://bucket/run/checkpoints/", **_kwargs()
    )

    # The shared upload step reads NPA_OUTPUT_PATH, so the in-job runtime sets it.
    assert script.startswith("export NPA_OUTPUT_PATH='s3://bucket/run/checkpoints/'")
    assert "/entrypoint.sh train" in script
    # No remote wrapper: this runs in the pod that is already here.
    assert not script.startswith("bash -lc")


def test_the_in_job_script_uses_the_images_own_interpreter() -> None:
    script = build_sonic_in_job_train_script(output_path="s3://bucket/x/", **_kwargs())

    assert "/isaac-sim/python.sh" in script
    assert "SONIC_RUN_REAL_TRAIN=1" in script


def test_training_knobs_reach_the_script() -> None:
    script = build_sonic_in_job_train_script(
        output_path="s3://bucket/x/", **_kwargs(num_envs=128, max_iterations=9, embodiment="g1")
    )

    assert "export SONIC_NUM_ENVS='128'" in script
    assert "export SONIC_MAX_ITERATIONS='9'" in script
    assert "export SONIC_EMBODIMENT='g1'" in script


def test_the_runtime_is_an_enum_member_so_the_cli_accepts_it() -> None:
    from npa.cli.workbench.sonic.helpers import TrainRuntime

    assert TrainRuntime("in-job") is TrainRuntime.in_job


def test_the_shipped_specs_ask_for_it() -> None:
    from pathlib import Path

    import yaml

    specs = Path(__file__).resolve().parents[3] / "npa/workflows/workbench/npa-workflows"
    for name in ("sonic-train.yaml", "sonic-locomotion-finetuning.yaml"):
        config = yaml.safe_load((specs / name).read_text(encoding="utf-8"))["config"]
        assert config["sonic_runtime"] == "in-job", name


def test_the_body_carries_the_images_eula_acceptance_rather_than_asserting_it() -> None:
    """Live job 323: the entrypoint refused with "Nothing has been downloaded".

    The SONIC image declares OMNI_KIT_ACCEPT_EULA=YES / ISAACSIM_ACCEPT_EULA=YES as docker ENV,
    because accepting NVIDIA's terms is a build-time decision its publisher made. SkyPilot's run
    shell does not inherit docker ENV, so the gate saw them unset.

    Reading /proc/1/environ forwards a decision the image already recorded. Exporting YES here
    would assert acceptance on someone else's behalf, and an image that did NOT accept would
    stop being refused — which is the whole point of the gate.
    """

    body = build_sonic_train_body(**_kwargs())

    assert "/proc/1/environ" in body
    for name in ("OMNI_KIT_ACCEPT_EULA", "ISAACSIM_ACCEPT_EULA"):
        assert name in body, name
    # Never asserted directly.
    assert "OMNI_KIT_ACCEPT_EULA=YES" not in body
    assert "ISAACSIM_ACCEPT_EULA=YES" not in body
    # Only filled when this shell does not already have it.
    assert 'printenv "$npa_eula"' in body


def test_the_carry_runs_before_the_entrypoint() -> None:
    body = build_sonic_train_body(**_kwargs())

    assert body.index("/proc/1/environ") < body.index("/entrypoint.sh train")


def test_acceptance_is_off_by_default() -> None:
    """Acceptance is the operator's to give, never the tool's to assume."""

    body = build_sonic_train_body(**_kwargs())

    assert "export OMNI_KIT_ACCEPT_EULA=YES" not in body
    assert "export ISAACSIM_ACCEPT_EULA=YES" not in body


def test_acceptance_is_exported_when_the_operator_gives_it() -> None:
    """Live job 327: the /proc/1 carry found nothing, because SkyPilot's pod replaces PID 1.

    The gate's own message asks for env entries on the pod or SkyPilot task, so the spec carries
    the operator's acceptance and the body exports it (EVIDENCE §R47).
    """

    body = build_sonic_train_body(accept_nvidia_eula=True, **_kwargs())

    assert "export OMNI_KIT_ACCEPT_EULA=YES" in body
    assert "export ISAACSIM_ACCEPT_EULA=YES" in body
    assert body.index("OMNI_KIT_ACCEPT_EULA=YES") < body.index("/entrypoint.sh train")


def test_the_shipped_specs_do_not_accept_on_the_operators_behalf() -> None:
    from pathlib import Path

    import yaml

    specs = Path(__file__).resolve().parents[3] / "npa/workflows/workbench/npa-workflows"
    for name in ("sonic-train.yaml", "sonic-locomotion-finetuning.yaml"):
        config = yaml.safe_load((specs / name).read_text(encoding="utf-8"))["config"]
        assert config["sonic_accept_nvidia_eula"] == "", name


def test_acceptance_is_a_value_so_a_spec_can_carry_it() -> None:
    """A bare boolean flag cannot be expressed by a toolRef argv, which is always flag+value."""

    from npa.cli.workbench.sonic.train import _is_affirmative
    from npa.orchestration.npa_workflow.catalog import TOOL_CATALOG

    argv = [str(part) for part in TOOL_CATALOG["workbench.sonic.train"].argv_template]
    assert argv[argv.index("--accept-nvidia-eula") + 1] == "{{config.sonic_accept_nvidia_eula}}"

    assert _is_affirmative("yes") and _is_affirmative(" YES ") and _is_affirmative("1")
    assert not _is_affirmative("") and not _is_affirmative("no") and not _is_affirmative("later")
