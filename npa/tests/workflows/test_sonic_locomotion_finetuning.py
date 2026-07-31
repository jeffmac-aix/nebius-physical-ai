from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
EXPECTED_WORKBENCH_IMAGE = "cr.eu-north1.nebius.cloud/<your-registry-id>/npa-genesis:0.4.6"
EXPECTED_RETARGETING_IMAGE = "cr.eu-north1.nebius.cloud/<your-registry-id>/npa-retargeting:0.1.1"
PIPELINE_YAML = (
    ROOT
    / "npa"
    / "src"
    / "npa"
    / "workflows"
    / "skypilot"
    / "sonic-locomotion-finetuning.yaml"
)
RETARGETING_YAML = (
    ROOT / "npa" / "src" / "npa" / "workflows" / "skypilot" / "retargeting.yaml"
)
MJLAB_YAML = ROOT / "npa" / "src" / "npa" / "workflows" / "skypilot" / "mjlab-eval.yaml"
# The raw sonic-export / sonic-eval / sonic-export-eval templates are retired; their
# npa.workflow specs are the surface now (each live-verified — see EVIDENCE §R4/§R5).
NPA_WORKFLOWS = ROOT / "npa" / "workflows" / "workbench" / "npa-workflows"
SONIC_TRAIN_STANDALONE_YAML = (
    ROOT
    / "npa"
    / "src"
    / "npa"
    / "workflows"
    / "skypilot"
    / "sonic-train-standalone.yaml"
)


def _docs(path: Path) -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8"))
        if doc is not None
    ]


def test_sonic_locomotion_pipeline_yaml_is_serial_and_uses_expected_tools() -> None:
    docs = _docs(PIPELINE_YAML)

    assert docs[0] == {"name": "sonic-locomotion-finetuning", "execution": "serial"}
    tasks = docs[1:]
    assert [task["name"] for task in tasks] == [
        "sonic-retarget-motion",
        "sonic-g1-finetune",
        "sonic-mujoco-eval",
    ]
    assert "npa workbench sonic retargeting run" in tasks[0]["run"]
    assert "/entrypoint.sh finetune" in tasks[1]["run"]
    assert "mujoco-eval" in tasks[2]["run"]


def test_sonic_locomotion_pipeline_uses_h100_mujoco_mvp_image() -> None:
    docs = _docs(PIPELINE_YAML)
    retarget, train, eval_task = docs[1:]

    assert retarget["resources"] == {
        "cloud": "kubernetes",
        "cpus": 4,
        "memory": 16,
        "image_id": "docker:${NPA_RETARGETING_IMAGE}",
    }
    assert retarget["envs"]["NPA_RETARGETING_IMAGE"] == EXPECTED_RETARGETING_IMAGE
    assert retarget["envs"]["SOURCE_FORMAT"] == "auto"
    assert retarget["envs"]["RETARGET_FRAME_RATE"] == "30"
    assert retarget["envs"]["RETARGET_SOURCE_FRAME_RATE"] == "120"
    assert retarget["envs"]["AWS_PROFILE"] == "nebius"
    assert retarget["envs"]["AWS_ENDPOINT_URL"] == "https://storage.eu-north1.nebius.cloud"
    assert train["resources"]["cloud"] == "nebius"
    assert train["resources"]["region"] == "eu-north1"
    assert train["resources"]["accelerators"] == "H100:1"
    assert train["resources"]["use_spot"] is True
    assert train["resources"]["image_id"] == (
        "docker:example.invalid/npa-sonic-mujoco:0.1.3-mvp"
    )
    assert eval_task["resources"]["cloud"] == "nebius"
    assert eval_task["resources"]["region"] == "eu-north1"
    assert eval_task["resources"]["accelerators"] == "H100:1"
    assert eval_task["resources"]["use_spot"] is True
    assert eval_task["resources"]["image_id"] == (
        "docker:example.invalid/npa-sonic-mujoco:0.1.3-mvp"
    )
    assert train["envs"]["POLICY_IMAGE"] == "example.invalid/npa-sonic-mujoco:0.1.3-mvp"
    assert eval_task["envs"]["POLICY_IMAGE"] == "example.invalid/npa-sonic-mujoco:0.1.3-mvp"
    assert train["envs"]["SONIC_GPU_TYPE"] == "h100"
    assert train["envs"]["SONIC_IMAGE_VARIANT"] == "sonic-mujoco-h100-mvp"
    assert train["envs"]["AWS_PROFILE"] == "nebius"
    assert train["envs"]["RETARGETED_MOTION_URI"].endswith("/retargeted/")
    assert train["envs"]["SONIC_TRAIN_MODE"] == "finetune"
    assert train["envs"]["SONIC_RUN_REAL_TRAIN"] == "1"
    assert eval_task["envs"]["SONIC_FINE_TUNED_CHECKPOINT_URI"].endswith(
        "/training/checkpoints/last.pt"
    )
    assert eval_task["envs"]["AWS_PROFILE"] == "nebius"
    assert eval_task["envs"]["SONIC_MUJOCO_STEPS"] == "64"


def test_sonic_workflow_materializer_resolves_images_and_s3_literals() -> None:
    from npa.workbench.sonic.workflow import materialize_sonic_workflow

    plan = materialize_sonic_workflow(
        PIPELINE_YAML,
        run_id="sonic-run",
        registry="registry.example/workbench",
        npa_image="registry.example/workbench/npa:tools",
        gpu_target="gpu-rtx6000",
        s3_endpoint="https://storage.example",
        s3_bucket="proof-bucket",
        s3_prefix="sonic-proof/sonic-run",
        accelerators="RTXPRO-6000-BLACKWELL-SERVER-EDITION:1",
    )
    docs = [doc for doc in yaml.safe_load_all(plan.yaml_text) if doc is not None]
    retarget, train, eval_task = docs[1:]

    assert retarget["resources"]["image_id"] == "docker:registry.example/workbench/npa-retargeting:0.1.1"
    assert train["resources"]["image_id"] == "docker:registry.example/workbench/npa-sonic:0.1.2-k8s-runtime"
    assert retarget["envs"]["AWS_PROFILE"] == "nebius"
    assert retarget["envs"]["AWS_ENDPOINT_URL"] == "https://storage.example"
    assert train["resources"]["cloud"] == "kubernetes"
    assert train["resources"]["accelerators"] == "RTXPRO-6000-BLACKWELL-SERVER-EDITION:1"
    assert eval_task["resources"]["image_id"] == (
        "docker:registry.example/workbench/npa-sonic:0.1.2-k8s-runtime"
    )
    assert eval_task["resources"]["cloud"] == "kubernetes"
    assert eval_task["resources"]["accelerators"] == "RTXPRO-6000-BLACKWELL-SERVER-EDITION:1"
    assert train["envs"]["SONIC_GPU_TYPE"] == "gpu-rtx6000"
    assert train["envs"]["SONIC_IMAGE_VARIANT"] == "sonic-k8s-host-mounted"
    assert train["envs"]["AWS_PROFILE"] == "nebius"
    assert train["envs"]["POLICY_IMAGE"] == (
        "registry.example/workbench/npa-sonic:0.1.2-k8s-runtime"
    )
    assert eval_task["envs"]["POLICY_IMAGE"] == (
        "registry.example/workbench/npa-sonic:0.1.2-k8s-runtime"
    )
    assert eval_task["envs"]["AWS_PROFILE"] == "nebius"
    assert train["envs"]["SONIC_TRAIN_OUTPUT_URI"] == "s3://proof-bucket/sonic-proof/sonic-run/training/"
    assert train["envs"]["RETARGETED_MOTION_URI"] == "s3://proof-bucket/sonic-proof/sonic-run/retargeted/"
    assert eval_task["envs"]["SONIC_FINE_TUNED_CHECKPOINT_URI"] == (
        "s3://proof-bucket/sonic-proof/sonic-run/training/checkpoints/last.pt"
    )
    assert eval_task["envs"]["SONIC_MUJOCO_OUTPUT_URI"] == (
        "s3://proof-bucket/sonic-proof/sonic-run/mujoco-eval/"
    )
    assert train["envs"]["AWS_ENDPOINT_URL"] == "https://storage.example"
    assert eval_task["envs"]["AWS_ENDPOINT_URL"] == "https://storage.example"
    for task in (retarget, train, eval_task):
        assert "${" not in task["resources"]["image_id"]
        assert "${" not in "\n".join(str(value) for value in task["envs"].values())
    assert "<your-" not in plan.yaml_text


def test_sonic_sdk_submit_passes_secret_envs(mocker) -> None:
    from npa.orchestration.skypilot.workflow import WorkflowResult
    from npa.workbench.sonic import workflow as sonic_workflow

    captured: dict[str, object] = {}

    def fake_submit_workflow(path, run_id, **kwargs):
        captured["content"] = path.read_text(encoding="utf-8")
        captured["run_id"] = run_id
        captured["kwargs"] = kwargs
        return WorkflowResult(status="SUBMITTED", job_id="42", returncode=0)

    mocker.patch.object(
        sonic_workflow,
        "_submit_skypilot_workflow",
        side_effect=fake_submit_workflow,
    )

    result = sonic_workflow.submit_sonic_workflow(
        SONIC_TRAIN_STANDALONE_YAML,
        run_id="sonic-run",
        registry="registry.example/workbench",
        gpu_target="l40s",
        s3_endpoint="https://storage.example",
        s3_bucket="proof-bucket",
        s3_prefix="sonic-proof/sonic-run",
        secret_envs=["AWS_ACCESS_KEY_ID"],
    )

    assert result.job_id == "42"
    assert captured["run_id"] == "sonic-run"
    assert captured["kwargs"]["secret_envs"] == ["AWS_ACCESS_KEY_ID"]
    assert "registry.example/workbench/npa-sonic:0.1.2" in str(captured["content"])


def test_sonic_workflow_materializer_supports_docker_payload_mode() -> None:
    from npa.workbench.sonic.workflow import materialize_sonic_workflow

    plan = materialize_sonic_workflow(
        SONIC_TRAIN_STANDALONE_YAML,
        run_id="sonic-run",
        registry="registry.example/workbench",
        gpu_target="l40s",
        s3_endpoint="https://storage.example",
        s3_bucket="proof-bucket",
        env_overrides={"SONIC_PAYLOAD_MODE": "docker"},
    )
    docs = [doc for doc in yaml.safe_load_all(plan.yaml_text) if doc is not None]
    task = docs[1]

    assert "image_id" not in task["resources"]
    assert task["envs"]["POLICY_IMAGE"] == "registry.example/workbench/npa-sonic:0.1.2"
    assert task["envs"]["SONIC_PAYLOAD_MODE"] == "docker"
    assert task["envs"]["SONIC_DOCKER_GPU_REQUEST"] == "all"
    assert '--gpus "${SONIC_DOCKER_GPU_REQUEST}"' in task["run"]
    assert "nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml" in task["run"]
    assert "NVIDIA_VISIBLE_DEVICES=${SONIC_DOCKER_GPU_REQUEST}" in task["run"]
    assert 'docker run --rm "${docker_gpu_args[@]}"' in task["run"]


def test_tool_yamls_match_registered_cli_surfaces() -> None:
    retarget_docs = _docs(RETARGETING_YAML)
    mjlab_docs = _docs(MJLAB_YAML)

    assert retarget_docs[0] == {"name": "retargeting", "execution": "serial"}
    assert retarget_docs[1]["name"] == "retarget-motion"
    assert "npa workbench sonic retargeting run" in retarget_docs[1]["run"]
    assert "accelerators" not in retarget_docs[1]["resources"]
    assert retarget_docs[1]["resources"]["image_id"] == "docker:${NPA_RETARGETING_IMAGE}"
    assert retarget_docs[1]["envs"]["NPA_RETARGETING_IMAGE"] == EXPECTED_RETARGETING_IMAGE
    assert retarget_docs[1]["envs"]["RETARGET_SOURCE_FRAME_RATE"] == "120"

    assert mjlab_docs[0] == {"name": "mjlab-eval", "execution": "serial"}
    assert mjlab_docs[1]["name"] == "mjlab-locomotion-eval"
    assert "npa workbench mjlab eval" in mjlab_docs[1]["run"]
    assert mjlab_docs[1]["resources"]["accelerators"] == "H100:1"
    assert mjlab_docs[1]["resources"]["image_id"] == "docker:${NPA_WORKBENCH_IMAGE}"
    assert mjlab_docs[1]["envs"]["NPA_WORKBENCH_IMAGE"] == EXPECTED_WORKBENCH_IMAGE


def test_sonic_export_and_eval_specs_invoke_the_real_cli_surfaces() -> None:
    """Replaces the raw-YAML `envs` assertions for the three retired templates.

    The equivalent contract on the npa.workflow side is: the spec declares the right
    ``toolRef``, wires every config key the toolRef's argv references (``load_spec``
    resolves them), and the *result path* is the declared artifact rather than a format
    word — the bug that made both eval stages succeed while writing nothing (EVIDENCE
    §R5).
    """

    from npa.orchestration.npa_workflow.interpreter import build_plan
    from npa.orchestration.npa_workflow.spec import load_spec

    export = load_spec(NPA_WORKFLOWS / "sonic-export.yaml")
    assert export.name == "sonic-export"
    assert export.states["export-onnx"].tool_ref == "workbench.sonic.export"
    export_argv = " ".join(build_plan(export, run_id="probe").steps[0].argv)
    assert "npa workbench sonic export" in export_argv
    assert "--checkpoint s3://" in export_argv and "--output s3://" in export_argv

    evaluate = load_spec(NPA_WORKFLOWS / "sonic-eval.yaml")
    assert evaluate.states["eval-onnx"].tool_ref == "workbench.sonic.eval"
    eval_argv = build_plan(evaluate, run_id="probe").steps[0].argv
    assert "npa workbench sonic eval" in " ".join(eval_argv)
    # `--output` is the RESULT PATH; `--output-format` is the format.
    assert eval_argv[eval_argv.index("--output") + 1].endswith("/eval.json")
    assert eval_argv[eval_argv.index("--output-format") + 1] == "json"
    assert eval_argv[eval_argv.index("--env") + 1] == "smoke"

    chained = load_spec(NPA_WORKFLOWS / "sonic-export-eval.yaml")
    steps = build_plan(chained, run_id="probe").steps
    assert [step.tool_ref for step in steps] == [
        "workbench.sonic.export",
        "workbench.sonic.eval",
    ]
    # The eval stage consumes exactly what the export stage produced.
    assert chained.config["onnx_uri"] in " ".join(steps[0].argv)
    assert chained.config["onnx_uri"] in " ".join(steps[1].argv)
    assert envs["CONTAINER_IMAGE_VARIANT"] == "sonic-l40s-baked"
    assert envs["CONTAINER_GPUS"] == "all"
    assert envs["CONTAINER_ARGS"] == "eval"
    assert envs["GPU"] == "L40S:1"

    run = task["run"]
    assert "npa workbench sonic export" in run
    assert "npa workbench sonic eval" in run
    assert "NPA_SONIC_E2E_METRICS_JSON_BEGIN" in run
    assert "--container-image" in run
    assert "--container-driver-capabilities" in run


def test_sonic_locomotion_assets_do_not_add_python_runner() -> None:
    scripts = {path.name for path in (ROOT / "npa" / "scripts").glob("run_*sonic*")}

    assert scripts == set()
