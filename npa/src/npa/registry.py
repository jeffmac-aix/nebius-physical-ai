"""Ownership-gated task registry provisioning."""

from __future__ import annotations

import re
from typing import Any

from npa.clients.config import list_projects, write_config
from npa.clients.nebius import (
    RegistryIdentity,
    create_registry,
    get_project_identity,
    get_registry_identity,
    list_registry_identities,
)
from npa.project_destroy import _project_ownership_operation
from npa.teardown_receipts import record_teardown_event

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,62}$")


def _registry_ref(identity: RegistryIdentity) -> str:
    if not identity.registry_fqdn:
        raise RuntimeError("registry is not ready: provider FQDN is absent")
    namespace = identity.registry_id.removeprefix("registry-")
    if not namespace:
        raise RuntimeError("registry has no usable immutable namespace")
    return f"{identity.registry_fqdn}/{namespace}"


def ensure_registry(
    *,
    project: str,
    name: str,
    profile: str = "",
    apply: bool = False,
) -> dict[str, Any]:
    """Plan or ensure one named registry inside an NPA-created project."""

    alias = str(project or "").strip()
    exact_name = str(name or "").strip()
    if not alias:
        raise RuntimeError("exact configured project alias is required")
    if not _NAME_RE.fullmatch(exact_name):
        raise RuntimeError(
            "registry name must be 3-63 lowercase letters, digits, or hyphens"
        )
    projects = list_projects()
    stanza = projects.get(alias)
    if not isinstance(stanza, dict):
        raise RuntimeError("registry provisioning requires an exact configured project")
    project_id = str(stanza.get("project_id") or "").strip()
    tenant_id = str(stanza.get("tenant_id") or "").strip()
    if not project_id or not tenant_id:
        raise RuntimeError("configured project is missing immutable ownership fields")
    identity = get_project_identity(
        project_id, tenant_id=tenant_id, profile=profile or None
    )
    if identity is None or identity.project_id != project_id:
        raise RuntimeError("exact configured project is absent")
    ownership = _project_ownership_operation(alias, project_id, tenant_id)
    if ownership is None:
        raise RuntimeError(
            "registry provisioning requires unique durable NPA project-creation proof"
        )
    matches = [
        item
        for item in list_registry_identities(project_id, profile=profile or None)
        if item.name == exact_name
    ]
    if len(matches) > 1:
        raise RuntimeError("registry name is ambiguous inside the exact project")
    selected = matches[0] if matches else None
    outcome = "existing" if selected else "planned_create"
    if apply and selected is None:
        selected = create_registry(project_id, exact_name, profile=profile or None)
        verified = get_registry_identity(
            selected.registry_id, profile=profile or None
        )
        if (
            verified is None
            or verified.project_id != project_id
            or verified.name != exact_name
        ):
            raise RuntimeError("created registry failed exact identity verification")
        selected = verified
        outcome = "created"
    payload: dict[str, Any] = {
        "outcome": outcome,
        "project": alias,
        "project_id": project_id,
        "registry_name": exact_name,
        "ownership_operation_id": ownership.operation_id,
        "applied": bool(apply),
    }
    if selected is not None:
        ref = _registry_ref(selected)
        payload.update(
            {
                "registry_id": selected.registry_id,
                "registry": ref,
                "verified": True,
            }
        )
        if apply:
            write_config(
                {
                    "projects": {
                        alias: {
                            "registry_id": selected.registry_id,
                            "registry_name": selected.name,
                            "container_registry": ref,
                        }
                    }
                }
            )
            record_teardown_event(
                phase="registry",
                resource=selected.registry_id,
                terminal_state="provisioned",
                project_alias=alias,
                project_id=project_id,
                identity={
                    "registry_id": selected.registry_id,
                    "registry_name": selected.name,
                    "project_id": project_id,
                    "ownership": "npa_disposable_project",
                    "project_operation_id": ownership.operation_id,
                },
                action={"kind": "ensure_exact_registry", "outcome": outcome},
            )
    return payload
