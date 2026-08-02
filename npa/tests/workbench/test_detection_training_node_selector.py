"""Which node a detection-training deploy asks for.

Live: a deploy defaulted to the `l40s` selector on a cluster whose GPU nodes are labelled
`gpu-rtx6000`. The pod stayed Unschedulable and the only symptom was `rollout status` timing
out — nothing in the output mentioned node labels, which is why this took a `kubectl get nodes`
to see (EVIDENCE §R46).
"""

from __future__ import annotations

import pytest

from npa.cli.workbench.detection_training import GPU_NODE_SELECTORS


def test_the_workbench_gpu_cluster_is_selectable() -> None:
    """The RTX PRO 6000 nodes this repo's own live cluster runs."""

    assert GPU_NODE_SELECTORS["rtxpro6000"] == "gpu-rtx6000"
    assert GPU_NODE_SELECTORS["rtx6000"] == "gpu-rtx6000"


def test_the_existing_shorthands_are_unchanged() -> None:
    assert GPU_NODE_SELECTORS["h100"] == "gpu-h100-sxm"
    assert GPU_NODE_SELECTORS["l40s"] == "gpu-l40s-d"


def test_every_selector_is_an_instance_type_label_value() -> None:
    """A label value, not a label: the key is always node.kubernetes.io/instance-type."""

    for shorthand, value in GPU_NODE_SELECTORS.items():
        assert "/" not in value, f"{shorthand} looks like a label key, not a value"
        assert value.startswith("gpu-"), shorthand


@pytest.mark.parametrize("unknown", ["b200", "", "GPU"])
def test_an_unknown_shorthand_has_no_selector_so_the_cli_can_refuse(unknown: str) -> None:
    assert GPU_NODE_SELECTORS.get(unknown) is None


def test_the_pull_secret_is_minted_before_falling_back_to_a_docker_config(monkeypatch) -> None:
    """A copied `~/.docker/config.json` holds a token that expires; a minted one does not.

    Same lesson as the LanceDB deploy (EVIDENCE §R41): a Deployment's kubelet re-pulls on every
    restart, long after whatever login produced that file.
    """

    from npa.cli.workbench import detection_training as dt

    minted: dict[str, str] = {}

    def fake_mint(secret_name, namespace, registry, **_kwargs):
        minted.update(name=secret_name, namespace=namespace, registry=registry)

    monkeypatch.setattr(
        "npa.workbench.service_kubernetes.ensure_registry_secret", fake_mint
    )

    def explode(_registry):
        raise AssertionError("must not read ~/.docker/config.json when minting works")

    monkeypatch.setattr(dt, "_docker_auth_config", explode)

    dt._ensure_image_pull_secret(
        image="cr.example.com/ns/npa-detection-training:1",
        secret_name="npa-registry",
        namespace="workbench",
        kubeconfig="",
    )

    assert minted == {
        "name": "npa-registry",
        "namespace": "workbench",
        "registry": "cr.example.com",
    }


def test_a_bare_image_name_needs_no_pull_secret(monkeypatch) -> None:
    from npa.cli.workbench import detection_training as dt

    monkeypatch.setattr(
        dt, "_docker_auth_config", lambda _r: (_ for _ in ()).throw(AssertionError("called"))
    )

    dt._ensure_image_pull_secret(
        image="npa-detection-training:1", secret_name="s", namespace="n", kubeconfig=""
    )
