"""No shipped file may point at a workflow path that does not exist.

Retiring the SkyPilot catalog deleted thirty templates, and the deletions were the easy part.
What kept slipping through was the *pointers*: a skill telling a reader to open a template that
had just been removed, a spec whose ``skypilotTwin`` named a deleted file, and — after a
careless bulk repoint — two specs whose ``skypilotTwin`` pointed at **themselves**.

None of that fails a test, breaks a build, or shows up in a diff review. It fails a human, later,
who follows the pointer and finds nothing there. One of them was worse than a dead link: a
troubleshooting skill shipped a Python snippet that opened the path and read ``doc["envs"]``,
which an npa.workflow spec does not have.

So the rule is mechanical: if a shipped file names a path under a workflow directory, that path
must exist. Historical records are exempt by design — EVIDENCE.md, CHANGELOG.md and DESIGN.md
exist precisely to talk about things that are gone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Files whose job is to describe history, where naming a deleted path is correct.
HISTORY = {"EVIDENCE.md", "CHANGELOG.md", "DESIGN.md", "PLAN.md"}

#: Directories that are searched for pointers.
SEARCH_ROOTS = ("skills", "docs", "npa/workflows", "npa/src/npa/workflows", "scripts")

#: Extensions worth searching. Python and shell are included because a broken path in an
#: executable snippet is the most expensive kind.
SEARCH_SUFFIXES = {".md", ".yaml", ".yml", ".py", ".sh", ".toml"}

#: Paths a file might name. Anchored at the repo root so a bare filename is not a false hit.
WORKFLOW_PATH = re.compile(
    r"npa/(?:src/npa/workflows/skypilot|workflows/workbench)/[A-Za-z0-9._/-]+\.ya?ml"
)

#: Placeholder paths that are illustrative rather than real.
PLACEHOLDER_MARKERS = ("<", ">", "{{", "${", "your-", "example-", "*")


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    for root in SEARCH_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in SEARCH_SUFFIXES:
                continue
            if path.name in HISTORY or "__pycache__" in path.parts:
                continue
            files.append(path)
    return sorted(files)


def _dangling_in(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):  # pragma: no cover - binary or unreadable
        return []
    bad = []
    for match in dict.fromkeys(WORKFLOW_PATH.findall(text)):
        if any(marker in match for marker in PLACEHOLDER_MARKERS):
            continue
        if not (REPO_ROOT / match).exists():
            bad.append(match)
    return bad


@pytest.mark.parametrize(
    "path", _candidate_files(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_no_shipped_file_points_at_a_missing_workflow(path: Path) -> None:
    dangling = _dangling_in(path)
    assert not dangling, (
        f"{path.relative_to(REPO_ROOT)} names workflow path(s) that do not exist: "
        f"{dangling}. Repoint them at the npa.workflow spec that replaced the template, or "
        f"delete the reference. A reader who follows one of these finds nothing."
    )


def test_a_spec_never_declares_itself_as_its_own_skypilot_twin() -> None:
    """``skypilotTwin`` records which raw template a spec replaced.

    A spec naming *itself* is not a harmless tautology — it is the fingerprint of a bulk
    path-repoint that rewrote the field's value along with everything else, which is exactly
    how two specs ended up claiming to be their own predecessor.
    """

    specs = REPO_ROOT / "npa/workflows/workbench/npa-workflows"
    offenders = []
    for spec in sorted(specs.glob("*.yaml")):
        text = spec.read_text(encoding="utf-8")
        named = re.findall(r"^\s*skypilotTwin:\s*(\S+)\s*$", text, re.M)
        # The plural form exists too, for a spec that absorbed more than one template — and it
        # is the one a singular-only regex quietly walks past.
        for block in re.findall(r"^[ \t]*skypilotTwins:\n((?:[ \t]*-[ \t]*\S+\n)+)", text, re.M):
            named.extend(re.findall(r"-[ \t]*(\S+)", block))
        # Compare full paths, not basenames: a spec and the raw template it replaced usually
        # SHARE a filename and differ only by directory, which is correct and common.
        own = spec.relative_to(REPO_ROOT).as_posix()
        if any(value.strip().lstrip("./") == own for value in named):
            offenders.append(spec.name)
    assert not offenders, f"specs declaring themselves as their own twin: {offenders}"


def test_the_guard_can_actually_fail(tmp_path: Path) -> None:
    """Guard the guard: a regex that matches nothing would pass everything."""

    victim = tmp_path / "doc.md"
    victim.write_text(
        "see npa/src/npa/workflows/skypilot/definitely-not-here.yaml\n", encoding="utf-8"
    )
    assert _dangling_in(victim) == ["npa/src/npa/workflows/skypilot/definitely-not-here.yaml"]


def test_the_guard_ignores_placeholders(tmp_path: Path) -> None:
    victim = tmp_path / "doc.md"
    victim.write_text(
        "npa/workflows/workbench/npa-workflows/<your-spec>.yaml\n", encoding="utf-8"
    )
    assert _dangling_in(victim) == []


def test_the_guard_is_looking_at_a_real_corpus() -> None:
    """A search that silently found no files would also pass everything."""

    assert len(_candidate_files()) > 200
