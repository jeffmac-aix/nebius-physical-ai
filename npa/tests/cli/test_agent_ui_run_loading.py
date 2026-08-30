"""Focused contracts for optional LeIsaac UI and smooth run hydration."""

from __future__ import annotations

from npa.cli.agent import rendered_agent_ui_html


def _ui() -> str:
    return rendered_agent_ui_html()


def test_leisaac_ui_is_default_off_with_persisted_explicit_opt_in() -> None:
    ui = _ui()
    start = ui.split("function startApp()", 1)[1].split(
        'if (document.readyState === "loading")', 1
    )[0]
    boot = ui.split("async function bootPage()", 1)[1].split(
        "function startPeriodicRefresh", 1
    )[0]

    assert 'id="enableLeIsaac"' in ui
    assert 'const LEISAAC_UI_STORAGE_KEY = "npa.agent.leisaac-ui-enabled.v1"' in ui
    assert 'window.localStorage.getItem(LEISAAC_UI_STORAGE_KEY) === "1"' in ui
    assert "if (leisaacUiEnabled())" in start
    assert "ensureLeIsaacTab(leisaacCapability);" in start
    assert "if (leisaacUiEnabled()) refreshLeIsaacCapability()" in boot
    assert 'id = "disableLeIsaac"' in ui


def test_boot_reuses_session_and_status_without_blocking_on_run_details() -> None:
    ui = _ui()
    boot = ui.split("async function bootPage()", 1)[1].split(
        "function startPeriodicRefresh", 1
    )[0]
    refresh = ui.split("async function refresh(options)", 1)[1].split(
        "function selectedCamera", 1
    )[0]

    assert "restoredSession = await restoreSession()" in boot
    assert "refresh({ session: restoredSession })" in boot
    assert "ensureFrankaRerunLoaded(lastSimVizStatus)" in boot
    assert "opts.session ? Promise.resolve(opts.session)" in refresh
    assert "opts.simViz ? Promise.resolve(opts.simViz)" in refresh
    assert "void loadRunDetails(activeRunId).catch" in refresh
    assert "await loadRunDetails(activeRunId)" not in refresh


def test_artifact_filters_reuse_complete_source_qualified_inventory() -> None:
    ui = _ui()
    wiring = ui.split(
        'for (const id of ["artifactStageFilter", "artifactTypeFilter", "artifactRoleFilter", "artifactSort"])',
        1,
    )[1].split('// "Find run (name or ID)" box', 1)[0]
    loader = ui.split("async function loadArtifactsForSelectedRun", 1)[1].split(
        "async function loadExactArtifactSource", 1
    )[0]

    assert "reuseInventory: true" in wiring
    assert "context.reuseInventory && activeArtifactInventoryPage" in loader
    assert 'String(activeArtifactInventoryPage.run_ref || "") === runRef' in loader
    assert "if (!data)" in loader
    assert "activeArtifactInventoryPage = data" in loader


def test_newer_run_selection_supersedes_stale_responses() -> None:
    ui = _ui()
    selector = ui.split("let selectedRunLoadGeneration", 1)[1].split(
        "function normalizeStageStatus", 1
    )[0]
    history = ui.split("async function loadWorkflowHistoryRun", 1)[1].split(
        "async function loadRunData", 1
    )[0]

    assert "const generation = ++selectedRunLoadGeneration" in selector
    assert "activeRunSelectionGeneration += 1" in selector
    assert "generation === selectedRunLoadGeneration" in selector
    assert "isCurrent," in selector
    assert "if (!isCurrent()) return null" in history
    assert "if (leisaacUiEnabled()) await refreshLeIsaacCapability()" in history
