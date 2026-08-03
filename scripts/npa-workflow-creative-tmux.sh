#!/usr/bin/env bash
# Beautify pass + creative npa.workflow pipeline + skill-index smoke in tmux.
# tmux new -s npa-workflow-creative ./scripts/npa-workflow-creative-tmux.sh
set -euo pipefail

export HOME="${HOME:-/home/ubuntu}"
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
[[ -f /home/ubuntu/bin/npa-cloud-env.sh ]] && source /home/ubuntu/bin/npa-cloud-env.sh

REPO="/home/ubuntu/nebius-physical-ai"
cd "$REPO"
PY="${REPO}/npa/.venv/bin/python"
NPA="${REPO}/npa/.venv/bin/npa"
export NPA_INTEGRATION_E2E=1
SPECS="${REPO}/npa/workflows/workbench/npa-workflows"
CREATIVE="${SPECS}/tokenfactory-cosmos-gate.yaml"

LOG="/tmp/npa-workflow-creative-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$LOG") 2>&1

echo "=== npa.workflow creative + beauty matrix log=${LOG} ==="
echo "branch: $(git branch --show-current) @ $(git rev-parse --short HEAD)"
echo "skills: author-npa-workflow + generate-npa-workflow"
echo "creative spec: ${CREATIVE}"

round=1
while true; do
  echo ""
  echo "========== ROUND ${round} $(date -u +%Y-%m-%dT%H:%M:%SZ) =========="
  FAILED=0

  echo "--- [1/8] skills index smoke (full guardrail) ---"
  if ! "${PY}" -m pytest npa/tests/guardrails/test_skills_index.py -q --timeout=120; then
    FAILED=1
  fi

  echo "--- [2/8] validate all golden npa.workflow YAMLs ---"
  for spec in "${SPECS}"/*.yaml; do
    base=$(basename "$spec")
    echo "validate: ${base}"
    if ! "${NPA}" workbench workflow validate-spec "${spec}"; then
      FAILED=1
    fi
  done

  echo "--- [3/8] creative pipeline plan (generate-npa-workflow golden) ---"
  if ! "${NPA}" workbench workflow plan-spec "${CREATIVE}" \
    --run-id "creative-r${round}" --assume-decision loop_back; then
    FAILED=1
  fi
  if ! "${NPA}" workbench workflow plan-spec "${CREATIVE}" \
    --run-id "creative-promote-r${round}" --assume-decision promote_checkpoint \
    --json | "${PY}" -c "import json,sys; p=json.load(sys.stdin); assert p['steps'][-1]['state']=='publish', p"; then
    FAILED=1
  fi

  echo "--- [4/8] plan dynamic specs (sim2real + creative) ---"
  for spec in "${SPECS}/sim2real-vlm-rl.yaml" "${CREATIVE}"; do
    base=$(basename "$spec")
    echo "plan: ${base}"
    if ! "${NPA}" workbench workflow plan-spec "${spec}" \
      --run-id "tmux-r${round}" --assume-decision loop_back; then
      FAILED=1
    fi
  done

  echo "--- [5/8] unit + audit + smoke ---"
  if ! "${PY}" -m pytest \
    npa/tests/orchestration/npa_workflow/ \
    npa/tests/smoke/test_npa_workflow_smoke.py \
    npa/tests/smoke/test_all_workflow_yamls.py \
    -q --timeout=180; then
    FAILED=1
  fi

  echo "--- [6/8] live e2e + infra ---"
  if ! "${PY}" -m pytest \
    npa/tests/e2e/test_npa_workflow_live_e2e.py \
    npa/tests/e2e/test_npa_workflow_live_infra.py \
    -q --timeout=300; then
    FAILED=1
  fi

  echo "--- [7/8] BDD100K + LanceDB ---"
  if ! "${PY}" -m pytest \
    npa/tests/workflows/test_bdd100k_pipeline.py \
    npa/tests/test_lancedb_bdd100k_import.py \
    npa/tests/test_lancedb_bdd100k_backfill.py \
    npa/tests/test_lancedb_bdd100k_mv.py \
    -q --timeout=300; then
    FAILED=1
  fi

  echo "--- [8/8] npa.workflow spec parse (all files) ---"
  if ! "${PY}" - <<'PY'; then
from pathlib import Path
import yaml

# The shipped raw SkyPilot task catalog is retired, so this walks the SPECS instead. It
# validates the surface pipelines are actually written on, and it does not go quietly
# vacuous the day the last raw template is deleted.
root = Path("npa/workflows/workbench/npa-workflows")
paths = sorted(root.glob("*.yaml"))
if len(paths) < 20:
    raise SystemExit(f"expected the spec catalog, found {len(paths)} files under {root}")
failed = []
for path in paths:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            failed.append(f"{path.name}: not a mapping")
            continue
        # Prefix, not equality: sim2real-gpu-cross-region-agent ships v0.0.1-beta on
        # purpose. What is being checked is "this is a spec at all", not the exact revision.
        if not str(doc.get("apiVersion") or "").startswith("npa.workflow/"):
            failed.append(f"{path.name}: apiVersion={doc.get('apiVersion')!r}")
        if not (doc.get("metadata") or {}).get("name"):
            failed.append(f"{path.name}: no metadata.name")
        if not doc.get("states"):
            failed.append(f"{path.name}: no states")
    except Exception as exc:
        failed.append(f"{path.name}: {exc}")
if failed:
    raise SystemExit("parse failures: " + ", ".join(failed))
print(f"npa.workflow spec parse OK ({len(paths)} files)")
PY
    FAILED=1
  fi

  if [[ "${FAILED}" -eq 0 ]]; then
    echo "--- ROUND ${round} ALL PASSED ---"
  else
    echo "--- ROUND ${round} FAILED ---"
  fi
  round=$((round + 1))
  sleep 600
done
