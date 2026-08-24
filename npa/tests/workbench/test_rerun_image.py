from __future__ import annotations

import subprocess

import pytest

from npa.clients.nebius import RegistryIdentity
from npa.workbench import rerun_image


def _registry(mocker) -> None:  # noqa: ANN001
    mocker.patch(
        "npa.workbench.rerun_image.list_projects",
        return_value={
            "demo": {
                "project_id": "project-a",
                "registry_id": "registry-a",
                "container_registry": "cr.example/a",
            }
        },
    )
    mocker.patch(
        "npa.workbench.rerun_image.get_registry_identity",
        return_value=RegistryIdentity(
            "registry-a", "task-registry", "project-a", "test", "cr.example"
        ),
    )


def _config(image_id: str = "sha256:" + "1" * 64) -> dict:
    return {
        "Id": image_id,
        "RepoDigests": [],
        "Config": {
            "User": "ubuntu",
            "Entrypoint": [
                "/opt/npa/docker/workbench/rerun-viewer/entrypoint.sh"
            ],
            "Labels": {
                rerun_image.ATTESTATION_LABEL: rerun_image.CONTRACT_VERSION
            },
        },
    }


def test_registry_token_uses_profile_and_scrubs_ambient_credentials(
    mocker, monkeypatch
) -> None:
    monkeypatch.setenv("NEBIUS_IAM_TOKEN", "stale-token")
    run = mocker.patch(
        "npa.workbench.rerun_image.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, "fresh-token\n", ""),
    )

    assert rerun_image._registry_token("task-profile") == "fresh-token"
    assert run.call_args.args[0] == [
        "nebius",
        "--profile",
        "task-profile",
        "iam",
        "get-access-token",
    ]
    assert "NEBIUS_IAM_TOKEN" not in run.call_args.kwargs["env"]


def test_build_uses_only_checked_in_dockerfile_and_task_registry(mocker, tmp_path) -> None:
    _registry(mocker)
    dockerfile = tmp_path / "npa/docker/workbench/rerun-viewer/Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("FROM scratch\n")
    (dockerfile.parent / "entrypoint.sh").write_text("#!/bin/sh\n")
    mocker.patch("npa.workbench.rerun_image.shutil.which", return_value="/usr/bin/docker")
    run = mocker.patch(
        "npa.workbench.rerun_image._run",
        return_value=subprocess.CompletedProcess([], 0, "", ""),
    )
    mocker.patch("npa.workbench.rerun_image._inspect_config", return_value=_config())

    result = rerun_image.build_rerun_viewer(
        project="demo", tag="validation-unit-test", repo_root=tmp_path
    )

    assert result["image"].startswith("cr.example/a/npa-rerun-viewer:")
    argv = run.call_args.args[0]
    assert argv[argv.index("--file") + 1] == "docker/workbench/rerun-viewer/Dockerfile"
    assert argv[-1] == "."
    assert run.call_args.kwargs["cwd"] == tmp_path / "npa"


def test_inspect_probes_exact_runtime_contract(mocker) -> None:
    _registry(mocker)
    mocker.patch("npa.workbench.rerun_image._inspect_config", return_value=_config())
    run = mocker.patch(
        "npa.workbench.rerun_image._run",
        return_value=subprocess.CompletedProcess([], 0, "compatible", ""),
    )

    result = rerun_image.inspect_rerun_viewer(
        project="demo", tag="validation-unit-test"
    )

    assert result["status"] == "compatible"
    assert len(result["inspection_digest"]) == 64
    argv = run.call_args.args[0]
    assert argv[:5] == ["docker", "run", "--rm", "--entrypoint", "/bin/sh"]
    assert "sudo -n true" in argv[-1]
    assert "command -v" in argv[-1]


def test_inspect_rejects_missing_exact_attestation(mocker) -> None:
    _registry(mocker)
    config = _config()
    config["Config"]["Labels"] = {}
    mocker.patch("npa.workbench.rerun_image._inspect_config", return_value=config)

    with pytest.raises(rerun_image.RerunImageError, match="exact SkyPilot"):
        rerun_image.inspect_rerun_viewer(
            project="demo", tag="validation-unit-test"
        )


def test_push_is_bound_to_inspected_bytes_and_uses_token_only_on_stdin(mocker) -> None:
    _registry(mocker)
    evidence = {
        "image_id": "sha256:" + "1" * 64,
        "inspection_digest": "2" * 64,
    }
    mocker.patch("npa.workbench.rerun_image._inspection", return_value=evidence)
    config = _config()
    config["RepoDigests"] = [
        "cr.example/a/npa-rerun-viewer@sha256:" + "3" * 64
    ]
    mocker.patch("npa.workbench.rerun_image._inspect_config", return_value=config)
    mocker.patch("npa.workbench.rerun_image._registry_token", return_value="secret-token")
    calls: list[tuple[list[str], str | None]] = []

    def run(argv, *, cwd=None, input_text=None):  # noqa: ANN001, ANN202, ARG001
        calls.append((list(argv), input_text))
        output = "digest: sha256:" + "3" * 64 if argv[:2] == ["docker", "push"] else ""
        return subprocess.CompletedProcess(argv, 0, output, "")

    mocker.patch("npa.workbench.rerun_image._run", side_effect=run)

    result = rerun_image.push_rerun_viewer(
        project="demo",
        tag="validation-unit-test",
        expected_image_id=evidence["image_id"],
        inspection_digest=evidence["inspection_digest"],
    )

    assert result["digest"] == "sha256:" + "3" * 64
    login = calls[0]
    assert login[0][:3] == ["docker", "login", "cr.example"]
    assert "secret-token" not in login[0]
    assert login[1] == "secret-token"


def test_push_refuses_changed_image_before_login(mocker) -> None:
    _registry(mocker)
    mocker.patch(
        "npa.workbench.rerun_image._inspection",
        return_value={
            "image_id": "sha256:" + "1" * 64,
            "inspection_digest": "2" * 64,
        },
    )
    mint = mocker.patch("npa.workbench.rerun_image._registry_token")

    with pytest.raises(rerun_image.RerunImageError, match="changed"):
        rerun_image.push_rerun_viewer(
            project="demo",
            tag="validation-unit-test",
            expected_image_id="sha256:" + "9" * 64,
            inspection_digest="2" * 64,
        )
    mint.assert_not_called()


def test_task_registry_must_match_exact_provider_identity(mocker) -> None:
    _registry(mocker)
    mocker.patch(
        "npa.workbench.rerun_image.get_registry_identity",
        return_value=RegistryIdentity(
            "registry-a", "task-registry", "different-project", "test", "cr.example"
        ),
    )

    with pytest.raises(rerun_image.RerunImageError, match="another project"):
        rerun_image.inspect_rerun_viewer(
            project="demo", tag="validation-unit-test"
        )
