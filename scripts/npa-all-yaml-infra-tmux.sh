#!/usr/bin/env bash
# Full workflow YAML matrix: npa.workflow + SkyPilot parse + live infra tests.
# tmux new -s npa-all-yaml-infra ./scripts/npa-all-yaml-infra-tmux.sh
set -euo pipefail

export HOME="${HOME:-/home/ubuntu}"
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
[[ -f /home/ubuntu/bin/npa-cloud-env.sh ]] && source /home/ubuntu/bin/npa-cloud-env.sh

REPO="/home/ubuntu/nebius-physical-ai"
cd "$REPO"
PY="${REPO}/npa/.venv/bin/python"
NPA="${REPO}/npa/.venv/bin/npa"
export NPA_INTEGRATION_E2E=1

NPA_SPECS="${REPO}/npa/workflows/workbench/npa-workflows"
# Retiring: only the templates that arrived from other PRs mid-sweep remain here, and the
# directory is scheduled for deletion. Counted, not walked, so the banner stays honest
# whether it holds two files or none.
SKY_SPECS="${REPO}/npa/src/npa/workflows/skypilot"

LOG="/tmp/npa-all-yaml-infra-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$LOG") 2>&1

echo "=== ALL workflow YAML infra matrix log=${LOG} ==="
echo "branch: $(git branch --show-current) @ $(git rev-parse --short HEAD)"
echo "npa.workflow specs: $(find "${NPA_SPECS}" -maxdepth 1 -name '*.yaml' | wc -l)"
echo "raw skypilot templates remaining: $(find "${SKY_SPECS}" -maxdepth 1 -name '*.yaml' 2>/dev/null | wc -l)"

round=1
while true; do
  echo ""
  echo "========== ROUND ${round} $(date -u +%Y-%m-%dT%H:%M:%SZ) =========="
  FAILED=0

  echo "--- [0/7] PR audit repro + audit_fixes pytest ---"
  AUDIT_DIR="${REPO}/.tmp-audit-repro"
  mkdir -p "${AUDIT_DIR}"
  cat > "${AUDIT_DIR}/cycle.yaml" <<'YAML'
apiVersion: npa.workflow/v0.0.1
kind: Workflow
metadata:
  name: cycle
config: {}
initial: a
states:
  a:
    run:
      shell: echo a
    next: b
  b:
    run:
      shell: echo b
    next: a
  done:
    terminal: true
YAML
  cat > "${AUDIT_DIR}/bad-token.yaml" <<'YAML'
apiVersion: npa.workflow/v0.0.1
kind: Workflow
metadata:
  name: bad-token
config:
  bucket: example-bucket
initial: x
states:
  x:
    run:
      shell: echo ok
    outputs:
      - uri: "{{config.does_not_exist}}"
    terminal: true
YAML
  cat > "${AUDIT_DIR}/bad-loop-max.yaml" <<'YAML'
apiVersion: npa.workflow/v0.0.1
kind: Workflow
metadata:
  name: bad-loop-max
config:
  n: not-an-int
initial: outer
states:
  outer:
    sequence: [inner]
    loop:
      max: "{{config.n}}"
    next: done
  inner:
    run:
      shell: echo inner
  done:
    terminal: true
YAML
  echo "audit: cycle.yaml must fail validate (exit 1)"
  if "${NPA}" workbench workflow validate-spec "${AUDIT_DIR}/cycle.yaml"; then
    echo "FAIL: cycle.yaml should not validate"
    FAILED=1
  fi
  echo "audit: bad-token.yaml must fail validate (exit 1)"
  if "${NPA}" workbench workflow validate-spec "${AUDIT_DIR}/bad-token.yaml"; then
    echo "FAIL: bad-token.yaml should not validate"
    FAILED=1
  fi
  echo "audit: bad-loop-max.yaml must fail validate (exit 1)"
  if "${NPA}" workbench workflow validate-spec "${AUDIT_DIR}/bad-loop-max.yaml"; then
    echo "FAIL: bad-loop-max.yaml should not validate"
    FAILED=1
  fi
  if ! "${PY}" -m pytest npa/tests/orchestration/npa_workflow/test_audit_fixes.py -q --timeout=120; then
    FAILED=1
  fi

  echo "--- [1/7] npa.workflow validate (all golden specs) ---"
  for spec in "${NPA_SPECS}"/*.yaml; do
    base=$(basename "$spec")
    echo "validate: ${base}"
    if ! "${NPA}" workbench workflow validate-spec "${spec}"; then
      FAILED=1
    fi
  done

  echo "--- [2/7] npa.workflow plan + scheduler (all golden specs) ---"
  RUN_ID="tmux-all-r${round}-$(date -u +%H%M%S)"
  for spec in "${NPA_SPECS}"/*.yaml; do
    base=$(basename "$spec")
    echo "plan: ${base}"
    extra=()
    if [[ "${base}" == "sim2real-vlm-rl.yaml" || "${base}" == "tokenfactory-cosmos-gate.yaml" ]]; then
      extra=(--assume-decision loop_back)
    fi
    if ! "${NPA}" workbench workflow plan-spec "${spec}" --run-id "${RUN_ID}" "${extra[@]}"; then
      FAILED=1
      continue
    fi
    if ! "${NPA}" workbench workflow run-spec "${spec}" \
      --run-id "${RUN_ID}" \
      --plan-only \
      --scheduler-plan \
      "${extra[@]}" \
      --json | "${PY}" -c "import json,sys; r=json.load(sys.stdin); assert r.get('scheduler',{}).get('tasks'), r"; then
      FAILED=1
    fi
  done

  echo "--- [3/7] npa.workflow spec parse (all files) ---"
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

  echo "--- [4/7] smoke: all workflow YAMLs ---"
  if ! "${PY}" -m pytest npa/tests/smoke/test_all_workflow_yamls.py -q --timeout=180; then
    FAILED=1
  fi

  echo "--- [5/7] npa.workflow unit + live infra ---"
  if ! "${PY}" -m pytest \
    npa/tests/orchestration/npa_workflow/ \
    npa/tests/smoke/test_npa_workflow_smoke.py \
    npa/tests/e2e/test_npa_workflow_live_e2e.py \
    npa/tests/e2e/test_npa_workflow_live_infra.py \
    -q --timeout=300; then
    FAILED=1
  fi

  echo "--- [6/7] SkyPilot workflow + BDD100K + guardrails ---"
  if ! "${PY}" -m pytest \
    npa/tests/workflows/test_bdd100k_pipeline.py \
    npa/tests/workflows/test_vlm_eval_workflow.py \
    npa/tests/workflows/test_token_factory_workflow.py \
    npa/tests/test_lancedb_bdd100k_import.py \
    npa/tests/test_lancedb_bdd100k_backfill.py \
    npa/tests/test_lancedb_bdd100k_mv.py \
    npa/tests/guardrails/test_workflow_image_check.py \
    -q --timeout=300; then
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
