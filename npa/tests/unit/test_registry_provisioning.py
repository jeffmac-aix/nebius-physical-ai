from __future__ import annotations

from npa.clients.nebius import ProjectIdentity, RegistryIdentity
from npa.registry import ensure_registry


def _owned_project(mocker) -> None:  # noqa: ANN001
    mocker.patch(
        "npa.registry.list_projects",
        return_value={
            "demo": {
                "project_id": "project-a",
                "tenant_id": "tenant-a",
                "region": "us-central1",
            }
        },
    )
    mocker.patch(
        "npa.registry.get_project_identity",
        return_value=ProjectIdentity(
            "project-a", "demo", "tenant-a", "us-central1", "test"
        ),
    )
    mocker.patch(
        "npa.registry._project_ownership_operation",
        return_value=mocker.Mock(operation_id="project-create-op"),
    )


def test_ensure_registry_dry_run_does_not_create_or_write(mocker) -> None:
    _owned_project(mocker)
    mocker.patch("npa.registry.list_registry_identities", return_value=())
    create = mocker.patch("npa.registry.create_registry")
    write = mocker.patch("npa.registry.write_config")
    record = mocker.patch("npa.registry.record_teardown_event")

    result = ensure_registry(project="demo", name="task-registry")

    assert result["outcome"] == "planned_create"
    assert result["applied"] is False
    create.assert_not_called()
    write.assert_not_called()
    record.assert_not_called()


def test_ensure_registry_creates_verifies_persists_and_records(mocker) -> None:
    _owned_project(mocker)
    mocker.patch("npa.registry.list_registry_identities", return_value=())
    created = RegistryIdentity(
        "registry-a", "task-registry", "project-a", "test", "cr.example"
    )
    mocker.patch("npa.registry.create_registry", return_value=created)
    mocker.patch("npa.registry.get_registry_identity", return_value=created)
    write = mocker.patch("npa.registry.write_config")
    record = mocker.patch("npa.registry.record_teardown_event")

    result = ensure_registry(project="demo", name="task-registry", apply=True)

    assert result["outcome"] == "created"
    assert result["registry"] == "cr.example/a"
    assert result["verified"] is True
    write.assert_called_once_with(
        {
            "projects": {
                "demo": {
                    "registry_id": "registry-a",
                    "registry_name": "task-registry",
                    "container_registry": "cr.example/a",
                }
            }
        }
    )
    record.assert_called_once()


def test_ensure_registry_reuses_only_same_name_in_exact_project(mocker) -> None:
    _owned_project(mocker)
    existing = RegistryIdentity(
        "registry-a", "task-registry", "project-a", "test", "cr.example"
    )
    mocker.patch(
        "npa.registry.list_registry_identities",
        return_value=(
            RegistryIdentity(
                "registry-b", "unrelated", "project-a", "test", "cr.example"
            ),
            existing,
        ),
    )
    create = mocker.patch("npa.registry.create_registry")
    mocker.patch("npa.registry.write_config")
    mocker.patch("npa.registry.record_teardown_event")

    result = ensure_registry(
        project="demo", name="task-registry", apply=True
    )

    assert result["outcome"] == "existing"
    assert result["registry_id"] == "registry-a"
    create.assert_not_called()


def test_ensure_registry_refuses_project_without_durable_ownership(mocker) -> None:
    _owned_project(mocker)
    mocker.patch("npa.registry._project_ownership_operation", return_value=None)
    inventory = mocker.patch("npa.registry.list_registry_identities")

    try:
        ensure_registry(project="demo", name="task-registry", apply=True)
    except RuntimeError as exc:
        assert "durable NPA project-creation proof" in str(exc)
    else:  # pragma: no cover - assertion helper
        raise AssertionError("unowned project must be rejected")
    inventory.assert_not_called()
