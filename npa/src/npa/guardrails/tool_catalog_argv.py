"""Guardrail helpers: a ``toolRef`` argv template must match a real CLI command.

Why this exists
---------------
An ``npa.workflow`` spec invokes a workbench tool by ``toolRef``. The engine turns
that reference into an argv list from
:data:`npa.orchestration.npa_workflow.catalog.TOOL_CATALOG` and runs it inside the
task pod. Nothing in the plan/render/submit path ever *checks* that the flags in an
argv template are flags the target CLI command actually accepts, so a template can
validate, plan and render perfectly and still crash the moment it runs on real
infrastructure.

That is not hypothetical. ``workbench.rl.policy_train`` renders
``npa workbench isaac-lab train --learning-rate ... --batch-size ... --input-path
...`` and none of those three options exist on that command (it takes
``--override``, ``--num-envs``, ``--steps``, ``--output-path``). The repo's own
``DESIGN.md`` §7 records the mismatch and deliberately left it unfixed; this module
turns it from tribal knowledge into a pinned, shrink-only guardrail.

This check replaces what the SkyPilot side of the three-tier contract used to give
us. While a raw SkyPilot YAML shipped for every tool, "the YAML declares an ``envs``
key per CLI flag" was a proxy for "the documented way to run this tool at scale
exposes its parameters". Once the npa.workflow spec is the only workflow surface,
the equivalent — and strictly sharper — question is "does the toolRef argv name real
CLI options?".
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import inspect
from typing import Any, Sequence


@dataclass(frozen=True)
class ResolvedCommand:
    """A CLI command reached by walking a Typer app tree with an argv prefix."""

    #: Dotted command path as a user types it, e.g. ``workbench sonic train``.
    path: str
    #: Fully qualified callback, e.g. ``npa.cli.workbench.sonic.train:train_cmd``.
    callback_ref: str
    #: Every long option the command accepts (``--flag`` forms only).
    flags: frozenset[str]


class ArgvResolutionError(RuntimeError):
    """Raised when an argv template cannot be mapped onto a CLI command."""


def _root_app() -> Any:
    from npa.cli.main import app

    return app


def _command_names(command_info: Any) -> tuple[str, ...]:
    """Return the names a Typer ``CommandInfo`` is reachable by."""

    explicit = getattr(command_info, "name", None)
    if explicit:
        return (str(explicit),)
    callback = getattr(command_info, "callback", None)
    if callback is None:
        return ()
    # Typer's default: the callback name with underscores turned into dashes.
    return (callback.__name__.lower().replace("_", "-"),)


def option_flags_for_callback(callback: Any) -> frozenset[str]:
    """Return every ``--long-option`` a Typer callback declares."""

    flags: set[str] = set()
    for param in inspect.signature(callback).parameters.values():
        for decl in getattr(param.default, "param_decls", ()) or ():
            for part in str(decl).split("/"):
                if part.startswith("--"):
                    flags.add(part)
    return frozenset(flags)


def resolve_argv_command(argv: Sequence[str]) -> ResolvedCommand:
    """Walk the ``npa`` Typer tree along ``argv`` and return the command it names.

    ``argv`` is a catalog ``argv_template``: ``["npa", "workbench", "sonic",
    "train", "--checkpoint", "{{config.x}}", ...]``. Only the leading
    non-option tokens are used for resolution; everything from the first ``-``
    onwards is treated as arguments.
    """

    tokens = [str(token) for token in argv]
    if not tokens or tokens[0] != "npa":
        raise ArgvResolutionError(
            f"argv template must start with 'npa', got {tokens[:1]!r}"
        )

    node = _root_app()
    walked: list[str] = []
    for token in tokens[1:]:
        if token.startswith("-"):
            break
        group = next(
            (
                info
                for info in node.registered_groups
                if str(getattr(info, "name", "")) == token
            ),
            None,
        )
        if group is not None:
            node = group.typer_instance
            walked.append(token)
            continue
        command = next(
            (
                info
                for info in node.registered_commands
                if token in _command_names(info)
            ),
            None,
        )
        if command is None:
            raise ArgvResolutionError(
                f"'npa {' '.join([*walked, token])}' is not a registered command or "
                "group; the toolRef argv names a CLI path that does not exist"
            )
        callback = command.callback
        walked.append(token)
        return ResolvedCommand(
            path=" ".join(walked),
            callback_ref=f"{callback.__module__}:{callback.__name__}",
            flags=option_flags_for_callback(callback),
        )

    raise ArgvResolutionError(
        f"'npa {' '.join(walked)}' resolved to a command group, not a command; the "
        "toolRef argv is missing a subcommand"
    )


def argv_template_flags(argv: Sequence[str]) -> tuple[str, ...]:
    """Return the long options a catalog argv template passes, in order."""

    return tuple(str(token) for token in argv if str(token).startswith("--"))


def argv_flag_drift(tool_ref: str, argv: Sequence[str]) -> tuple[str, ...]:
    """Return the argv flags that the target CLI command does not accept.

    An empty tuple means the template can actually run. Raises
    :class:`ArgvResolutionError` when the argv does not name a CLI command at all
    (which is a harder failure than flag drift).
    """

    del tool_ref  # kept for a readable call site / future per-tool exemptions
    command = resolve_argv_command(argv)
    return tuple(flag for flag in argv_template_flags(argv) if flag not in command.flags)


def catalog_argv_drift() -> dict[str, tuple[str, ...]]:
    """Map every non-stub catalog toolRef to its unaccepted flags.

    Stub entries are excluded: a ``stub=True`` tool is documented as not yet
    executing real work, so holding its argv to a live CLI signature would pin
    placeholder shapes.
    """

    from npa.orchestration.npa_workflow.catalog import TOOL_CATALOG

    drift: dict[str, tuple[str, ...]] = {}
    for tool_ref, entry in TOOL_CATALOG.items():
        if entry.stub:
            continue
        if not entry.argv_template or str(entry.argv_template[0]) != "npa":
            # Not an `npa ...` invocation (e.g. a bare interpreter call); out of
            # scope for CLI-signature checking.
            continue
        try:
            unaccepted = argv_flag_drift(tool_ref, entry.argv_template)
        except ArgvResolutionError as exc:
            drift[tool_ref] = (f"<unresolvable: {exc}>",)
            continue
        if unaccepted:
            drift[tool_ref] = unaccepted
    return drift


def import_callback(module_name: str, callback_name: str) -> Any:
    """Import a CLI callback by module and attribute name."""

    return getattr(import_module(module_name), callback_name)


__all__ = [
    "ArgvResolutionError",
    "ResolvedCommand",
    "argv_flag_drift",
    "argv_template_flags",
    "catalog_argv_drift",
    "import_callback",
    "option_flags_for_callback",
    "resolve_argv_command",
]
