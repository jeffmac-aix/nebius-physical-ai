"""Guardrail: every ``toolRef`` argv template must name real CLI options.

An ``npa.workflow`` spec's only way to invoke a workbench tool is a ``toolRef``,
which the engine expands into an argv list from ``TOOL_CATALOG`` and runs inside the
task pod. Nothing in validate → plan → render checks those flags against the CLI
command they will be handed to, so a template can be perfectly valid and still die
with ``No such option`` on real infrastructure. ``DESIGN.md`` §7 records exactly
that for ``workbench.rl.policy_train``.

Retiring the raw SkyPilot catalog makes this the load-bearing check: while a
SkyPilot YAML shipped per tool, the three-tier contract's "the YAML declares an
``envs`` key for each CLI flag" was a proxy for "the documented way to run this tool
at scale exposes its parameters". With the spec as the only workflow surface, the
sharper question is whether the toolRef argv can run at all.
"""

from __future__ import annotations

import pytest

from npa.guardrails.tool_catalog_argv import (
    ArgvResolutionError,
    argv_flag_drift,
    argv_template_flags,
    catalog_argv_drift,
    resolve_argv_command,
)
from npa.orchestration.npa_workflow.catalog import TOOL_CATALOG

#: Catalog entries whose argv is not an ``npa ...`` invocation (an inline
#: ``python3 -c`` snippet or a ``bash -c`` wrapper). Their flags cannot be checked
#: against a Typer signature. Pinned so the set can shrink but not silently grow.
NON_CLI_ARGV = frozenset(
    {
        "workbench.data_transform.improvement_summary",
        "workbench.data_transform.rollout_contract",
        "workbench.dataset.report_rejection",
        "workbench.dataset.write_quality_decision",
        "workbench.lancedb.backfill_cpu_bundle",
        "workbench.lancedb.create_failure_views",
        "workbench.rl.publish_policy",
        "workbench.rl.report_failure",
        "workbench.rl.write_success_decision",
        "workbench.scenario_gen.write_hardening_decision",
        "workbench.sim2real.write_decision",
        "workbench.sim2real_envgen.raw_shard",
    }
)


def _cli_backed_tool_refs() -> list[str]:
    return sorted(
        tool_ref
        for tool_ref, entry in TOOL_CATALOG.items()
        if not entry.stub
        and entry.argv_template
        and str(entry.argv_template[0]) == "npa"
    )


def test_no_tool_ref_argv_passes_a_flag_its_cli_rejects() -> None:
    """The whole catalog must be runnable, not merely renderable."""

    drift = catalog_argv_drift()
    assert not drift, (
        "toolRef argv templates pass options their CLI command does not accept; "
        "these specs would crash in the pod after a successful render:\n"
        + "\n".join(f"  {ref}: {flags}" for ref, flags in sorted(drift.items()))
    )


@pytest.mark.parametrize("tool_ref", _cli_backed_tool_refs())
def test_tool_ref_argv_resolves_to_a_real_command(tool_ref: str) -> None:
    command = resolve_argv_command(TOOL_CATALOG[tool_ref].argv_template)
    assert command.path.startswith("workbench") or command.path.startswith("soperator")
    assert command.flags, f"{tool_ref}: {command.path} declares no options"


def test_non_cli_argv_entries_are_pinned() -> None:
    """Inline python/bash toolRefs are exempt; the exemption list may only shrink."""

    actual = {
        tool_ref
        for tool_ref, entry in TOOL_CATALOG.items()
        if not entry.stub
        and entry.argv_template
        and str(entry.argv_template[0]) != "npa"
    }
    unexpected = actual - NON_CLI_ARGV
    assert not unexpected, (
        "new non-CLI toolRef argv templates are unchecked by this guardrail; "
        f"prefer an `npa ...` invocation, or pin them explicitly: {sorted(unexpected)}"
    )
    stale = NON_CLI_ARGV - actual
    assert not stale, f"NON_CLI_ARGV lists entries that no longer exist: {sorted(stale)}"


def test_guardrail_detects_an_invented_flag() -> None:
    """Negative control: the check must fail on a flag the command does not have."""

    argv = ["npa", "workbench", "mjlab", "eval", "--totally-invented-flag", "x"]

    assert argv_flag_drift("fixture.invented", argv) == ("--totally-invented-flag",)


def test_guardrail_detects_a_nonexistent_command() -> None:
    """Negative control: a bad CLI path is a harder failure than flag drift."""

    with pytest.raises(ArgvResolutionError, match="not a registered command"):
        resolve_argv_command(["npa", "workbench", "no-such-tool", "run", "--x", "1"])


def test_guardrail_rejects_a_group_without_a_subcommand() -> None:
    with pytest.raises(ArgvResolutionError, match="missing a subcommand"):
        resolve_argv_command(["npa", "workbench", "mjlab"])


def test_argv_template_flags_ignores_values() -> None:
    argv = ["npa", "workbench", "mjlab", "eval", "--input-path", "--not-a-flag-value"]

    # A value that merely *looks* like a flag is still reported; that is deliberate,
    # since the CLI would also read it as one.
    assert argv_template_flags(argv) == ("--input-path", "--not-a-flag-value")


def test_isaac_lab_rl_tool_refs_use_the_real_flag_names() -> None:
    """Pin the two fixes this guardrail found (DESIGN §7 drift).

    ``policy_train`` used ``--learning-rate`` / ``--batch-size`` / ``--input-path``
    and ``evaluate_policy`` used ``--episodes``; none of those exist on
    ``npa workbench isaac-lab {train,eval}``.
    """

    train = argv_template_flags(TOOL_CATALOG["workbench.rl.policy_train"].argv_template)
    assert "--learning-rate" not in train and "--batch-size" not in train
    assert "--input-path" not in train
    assert {"--task", "--steps", "--num-envs", "--override", "--data-path", "--output-path"} <= set(
        train
    )

    evaluate = argv_template_flags(
        TOOL_CATALOG["workbench.rl.evaluate_policy"].argv_template
    )
    assert "--episodes" not in evaluate
    assert "--num-episodes" in evaluate
