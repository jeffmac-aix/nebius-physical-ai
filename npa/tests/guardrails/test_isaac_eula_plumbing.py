"""Every automated path that runs an Isaac image must carry the operator's EULA acceptance.

The four Isaac workbench images ship no NVIDIA Isaac Sim or Isaac Lab. They fetch it on
first run and **refuse** (exit 78) unless the operator has set both
``OMNI_KIT_ACCEPT_EULA`` and ``ISAACSIM_ACCEPT_EULA``. That refusal is the legal mechanism
and is tested directly elsewhere.

The corollary is what this file guards, and it is easy to miss: any automated path that
launches one of those images has to *carry* that acceptance, or it simply cannot run them.
The serverless golden eval found this the expensive way — a real submitted job failed with

    isaac-bootstrap: refusing to download NVIDIA Isaac Sim / Isaac Lab.
    Not accepted (unset or not YES): OMNI_KIT_ACCEPT_EULA ISAACSIM_ACCEPT_EULA

which is correct behaviour and a useless test run. At that point none of the twelve
SkyPilot task templates that use an Isaac image declared the variables either.

The fix is emphatically NOT to hardcode ``YES`` anywhere in the repo: that would be us
accepting on the operator's behalf, which is precisely what the re-architecture exists to
avoid. Instead the variables are declared empty (so a task fails closed, with the
actionable message) and the operator supplies them at launch. These tests pin both halves:
the plumbing exists, and it does not pre-accept.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SKYPILOT_DIR = REPO_ROOT / "npa" / "src" / "npa" / "workflows" / "skypilot"
EULA_VARS = ("OMNI_KIT_ACCEPT_EULA", "ISAACSIM_ACCEPT_EULA")
#: Images whose entrypoints reach Isaac through the bootstrap shim.
ISAAC_IMAGE_MARKERS = ("npa-isaac-lab", "npa-sonic")


def _isaac_templates() -> list[Path]:
    return sorted(
        path
        for path in SKYPILOT_DIR.glob("*.yaml")
        if any(marker in path.read_text(encoding="utf-8") for marker in ISAAC_IMAGE_MARKERS)
    )


def test_there_are_isaac_templates_to_check() -> None:
    """Guard the guard: a rename that empties the set must not silently pass."""
    assert len(_isaac_templates()) >= 10


@pytest.mark.parametrize("path", _isaac_templates(), ids=lambda p: p.name)
def test_isaac_templates_declare_eula_acceptance(path: Path) -> None:
    """Declared, so the task documents what it needs and fails closed without it."""
    text = path.read_text(encoding="utf-8")
    for var in EULA_VARS:
        assert var in text, (
            f"{path.name} runs an Isaac image but never declares {var}. The image will "
            f"exit 78 at first use of /isaac-sim/python.sh. Declare it empty in `envs:` "
            f"and supply it at launch with --env {var}=YES."
        )


@pytest.mark.parametrize("path", _isaac_templates(), ids=lambda p: p.name)
def test_isaac_templates_declare_eula_in_every_envs_block(path: Path) -> None:
    """A multi-task file needs it in EVERY task, not just the first.

    isaac-lab-rl-sweep.yaml has four; sonic-locomotion-finetuning.yaml three. Missing one
    means that stage alone dies, which is a maddening way to discover the problem.
    """
    text = path.read_text(encoding="utf-8")
    envs_blocks = len(re.findall(r"(?m)^envs:\n", text))
    for var in EULA_VARS:
        assert text.count(f"{var}:") >= envs_blocks, (
            f"{path.name} has {envs_blocks} `envs:` block(s) but declares {var} "
            f"{text.count(f'{var}:')} time(s); every task that runs an Isaac image needs it"
        )


@pytest.mark.parametrize("path", _isaac_templates(), ids=lambda p: p.name)
def test_templates_do_not_pre_accept_the_licence(path: Path) -> None:
    """The declaration must be EMPTY. Pre-accepting would gut the whole mechanism."""
    for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if not isinstance(document, dict):
            continue
        for var, value in (document.get("envs") or {}).items():
            if var in EULA_VARS:
                assert value in ("", None), (
                    f"{path.name} pre-accepts NVIDIA's licence ({var}={value!r}). "
                    f"Acceptance is the operator's to give at launch; baking it here is "
                    f"the exact thing the runtime-fetch architecture exists to avoid."
                )


def test_serverless_runner_forwards_but_never_invents_acceptance() -> None:
    """The golden-eval submitter passes the caller's acceptance through, and only that."""
    from npa.smoke import serverless_runner

    assert set(serverless_runner.ISAAC_EULA_VARS) == set(EULA_VARS)

    source = Path(serverless_runner.__file__).read_text(encoding="utf-8")
    instructions = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    for var in EULA_VARS:
        assert f'"{var}": "YES"' not in instructions, "must not pre-accept on the operator's behalf"
    assert 'os.environ[name]' in instructions, "acceptance must come from the caller's environment"


def test_serverless_runner_omits_unset_acceptance(monkeypatch) -> None:
    """Absent acceptance must stay absent — not become an empty string that looks set."""
    from npa.smoke import serverless_runner

    for var in EULA_VARS:
        monkeypatch.delenv(var, raising=False)
    assert serverless_runner.isaac_eula_env() == {}

    monkeypatch.setenv("OMNI_KIT_ACCEPT_EULA", "YES")
    assert serverless_runner.isaac_eula_env() == {"OMNI_KIT_ACCEPT_EULA": "YES"}


# --------------------------------------------------------------------------------------
# Bootstrap ordering in SkyPilot setup blocks
# --------------------------------------------------------------------------------------


def _templates_asserting_the_isaac_tree() -> list[Path]:
    marker = "test -f /workspace/isaaclab/scripts"
    return sorted(
        path for path in SKYPILOT_DIR.glob("*.yaml") if marker in path.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("path", _templates_asserting_the_isaac_tree(), ids=lambda p: p.name)
def test_isaac_tree_assertion_comes_after_the_bootstrap_is_triggered(path: Path) -> None:
    """Asserting the Isaac Lab tree exists before fetching it is a guaranteed FAILED_SETUP.

    The isaaclab wheel ships the library but no ``scripts/``, so /workspace/isaaclab is
    populated on first use of the interpreter — not by the image. A real
    ``sky launch`` of isaac-lab-rl-train-rtxpro-smoke failed exactly this way: the setup
    block ran ``test -f .../rsl_rl/train.py`` before anything had invoked
    ``/isaac-sim/python.sh``.
    """
    text = path.read_text(encoding="utf-8")
    for match in re.finditer(r"(?m)^\s*test -f /workspace/isaaclab/scripts\S*", text):
        preceding = text[: match.start()]
        # The bootstrap fires on any invocation of the Isaac interpreter.
        last_setup = preceding.rfind("setup:")
        assert last_setup != -1, f"{path.name}: tree assertion outside a setup block"
        block = preceding[last_setup:]
        assert '"${PYTHON_BIN}"' in block or "isaac-bootstrap" in block, (
            f"{path.name}: asserts the Isaac Lab tree before anything triggers the "
            f"bootstrap that fetches it — this fails setup every time"
        )


def test_the_shared_serverless_builder_forwards_acceptance() -> None:
    """It belongs in the SHARED builder, not one caller.

    Every CLI serverless path (isaac_lab, groot, genesis, cosmos, fiftyone) and the
    golden-eval runner go through build_serverless_job_env, so putting the forwarding there
    is what makes `npa workbench isaac-lab train --runtime serverless` work too. An earlier
    version fixed only the golden-eval runner and left the CLI path broken.
    """
    import os

    from npa.serverless_common.env import build_serverless_job_env

    previous = {var: os.environ.get(var) for var in EULA_VARS}
    try:
        for var in EULA_VARS:
            os.environ[var] = "YES"
        env = build_serverless_job_env(output_path="s3://b/p/")
        for var in EULA_VARS:
            assert env[var] == "YES", var

        for var in EULA_VARS:
            del os.environ[var]
        env = build_serverless_job_env(output_path="s3://b/p/")
        for var in EULA_VARS:
            assert var not in env, f"{var} must stay absent, not become an empty string"
    finally:
        for var, value in previous.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value


def test_an_explicit_caller_value_beats_the_forwarded_one(monkeypatch) -> None:
    """extra_env is applied after the forward, so a caller can still override."""
    from npa.serverless_common.env import build_serverless_job_env

    monkeypatch.setenv("OMNI_KIT_ACCEPT_EULA", "YES")
    env = build_serverless_job_env(
        output_path="s3://b/p/", extra_env={"OMNI_KIT_ACCEPT_EULA": "no"}
    )
    assert env["OMNI_KIT_ACCEPT_EULA"] == "no"


def test_every_cli_serverless_path_uses_the_shared_builder() -> None:
    """If a submitter hand-rolls its env, it silently loses the acceptance forwarding."""
    cli_root = REPO_ROOT / "npa" / "src" / "npa" / "cli"
    submitters = sorted(
        path
        for path in cli_root.rglob("*.py")
        if "create_job(" in path.read_text(encoding="utf-8")
    )
    assert submitters, "no serverless submitters found — has the API changed?"
    for path in submitters:
        text = path.read_text(encoding="utf-8")
        assert "build_serverless_job_env" in text, (
            f"{path.relative_to(REPO_ROOT)} submits a serverless job without using "
            f"build_serverless_job_env, so it will not carry the operator's Isaac EULA "
            f"acceptance (nor the standard S3/HF wiring)"
        )
