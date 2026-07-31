"""A toolRef may declare third-party CLIs it shells out to.

`npa workbench cosmos fetch` runs `huggingface-cli`. The retired `cosmos3-ea-fetch.yaml`
pip-installed `huggingface_hub[cli]` in its setup — one line that turned out to be the only
load-bearing part of its ~60-line preamble. The twin dropped it and the stage failed live with
``checkpoint download failed: [Errno 2] No such file or directory: 'huggingface-cli'``
(job 226), after `check-access` had already SUCCEEDED.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from npa.orchestration.npa_workflow.interpreter import build_plan
from npa.orchestration.npa_workflow.skypilot_render import (
    TOOL_REF_PIP_REQUIREMENTS,
    SkypilotRenderOptions,
    assert_no_unresolved_placeholders,
    render_pip_requirements_setup,
    render_skypilot_yaml,
    tool_pip_requirements,
)
from npa.orchestration.npa_workflow.spec import load_spec

SPECS = Path(__file__).resolve().parents[4] / "npa" / "workflows" / "workbench" / "npa-workflows"


def test_requirements_resolve_by_exact_ref_and_by_prefix() -> None:
    assert tool_pip_requirements("workbench.cosmos.fetch") == (
        ("huggingface-cli", "huggingface_hub[cli]>=0.23,<1.0"),
    )
    # An unrelated tool declares nothing.
    assert tool_pip_requirements("workbench.mjlab.eval") == ()
    assert tool_pip_requirements("") == ()


def test_install_is_conditional_on_the_executable_being_absent() -> None:
    """A purpose-built image that already ships the CLI must be left alone."""

    setup = render_pip_requirements_setup(tool_pip_requirements("workbench.cosmos.fetch"))

    assert "if ! command -v huggingface-cli >/dev/null 2>&1; then" in setup
    assert "npa_pip_install 'huggingface_hub[cli]>=0.23,<1.0'" in setup
    # Uses the shared helper, so it inherits the PEP 668 fallbacks default_npa_setup defines.
    assert setup.count("npa_pip_install") == 1


def test_no_requirements_renders_nothing() -> None:
    assert render_pip_requirements_setup(()) == ""


def test_every_declared_requirement_names_an_executable_and_a_spec() -> None:
    for tool_ref, requirements in TOOL_REF_PIP_REQUIREMENTS.items():
        assert requirements, tool_ref
        for executable, requirement in requirements:
            assert executable and " " not in executable, (tool_ref, executable)
            # A pip requirement, not a bare import name.
            assert any(marker in requirement for marker in "=<>[") or requirement.isidentifier()


def test_shipped_cosmos_spec_renders_the_installer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NPA_SRC_S3_URI", "s3://example-bucket/prefix/npa")
    spec = load_spec(SPECS / "cosmos-fetch.yaml")
    plan = build_plan(spec, run_id="pip-requirements-check")

    text = render_skypilot_yaml(
        spec,
        plan,
        run_id="pip-requirements-check",
        options=SkypilotRenderOptions(image_overrides={"*": ""}),
    )

    docs = [doc for doc in yaml.safe_load_all(text) if doc]
    stages = [doc for doc in docs if "run" in doc]
    assert len(stages) == 2
    for doc in stages:
        assert "huggingface_hub[cli]" in doc["setup"], doc["name"]
    assert_no_unresolved_placeholders(text)
