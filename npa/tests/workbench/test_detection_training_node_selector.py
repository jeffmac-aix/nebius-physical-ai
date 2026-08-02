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
