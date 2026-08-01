# Workbench Container Packaging

Canonical contract for packaging workbench containers correctly, securely, and
with the right runtime features exposed. Machine-readable rules live in
`npa/docker/workbench/packaging-contract.yaml` (enforced by unit tests).

## Inventory

All first-class images live under `npa/docker/workbench/`:

| Image / role | Dockerfile | Default exposure |
| --- | --- | --- |
| `npa-lerobot` | `lerobot/Dockerfile` | FastAPI server `:8080` |
| `npa-lerobot-policy` | `lerobot-policy/Dockerfile` | entrypoint modes (serve/train/eval) |
| `npa-genesis` | `genesis/Dockerfile` | job shell (CLI supplies command) |
| `npa-isaac-lab` | `isaac-lab/Dockerfile` | job shell |
| `npa-cosmos` | `cosmos/Dockerfile` | job shell; server built but not default CMD |
| `npa-groot` | `groot/Dockerfile` | job shell; `EXPOSE 8080` |
| `npa-fiftyone` | `fiftyone/Dockerfile` | job shell; `EXPOSE 5151` |
| `npa-lancedb` | `lancedb/Dockerfile` | uvicorn `:8686` |
| `npa-sonic` | `sonic/Dockerfile` | `/entrypoint.sh` modes |
| `npa-detection-training` | `detection-training/Dockerfile` | uvicorn `:8790` |
| `npa-retargeting` | `retargeting/Dockerfile` | job shell |
| Sim2Real stack | `sim2real-*/`, `cosmos3-reason/`, `lerobot-vlm-rl/` | workflow modules |
| Base CUDA 13 | `base/cuda13-b300/Dockerfile` | build base only |

BYOF images (`npa-byof:<run-id>`) are **ad-hoc** and are not registered in
`CONTAINER_IMAGE_NAMES` until promoted to Tier 2 (see
`docs/architecture/oss-onboarding-ladder.md`).

## Packaging tiers

Every Dockerfile must declare one of:

| Tier | `kind` | ENTRYPOINT expectation | Examples |
| --- | --- | --- | --- |
| **Service** | `service` | Starts the HTTP service (or entrypoint that does) | lerobot, lancedb, detection-training, lerobot-policy |
| **Job** | `job` | Runs a workflow/CLI module with explicit CMD | sonic, sim2real-eval, cosmos3-reason, lerobot-vlm-rl |
| **Interactive** | `interactive` | `/bin/bash` allowed only when CLI always overrides CMD | genesis, isaac-lab, fiftyone, cosmos, groot, retargeting |

Do not ship a service-capable image as `interactive` without documenting why
(deploy path must override CMD). Prefer promoting Cosmos/GR00T to `service`
when the FastAPI server is the primary product surface.

## Security baseline

Required for all workbench images:

1. **Non-root runtime** — final `USER` is non-root (`ubuntu` / uid 1000 or
   documented equivalent). Build stages may use root.
2. **No secrets in layers** — credentials via env / K8s `secretRef` only.
3. **Digest-pinned bases** where the registry allows anonymous or CI digest
   resolution (see `docs/security/image-reproducibility.md`). Document tag-only
   exceptions (e.g. NGC Isaac Lab) with a TODO.
4. **CVE scanning** — Trivy via `.github/workflows/image-security-scan.yml`.
5. **Capability drops at deploy** — K8s `securityContext` should drop `ALL`,
   set `allowPrivilegeEscalation: false`, and use `RuntimeDefault` seccomp
   (detection-training is the reference template).

Strongly recommended for `service` images:

- `HEALTHCHECK` against `/health` (or documented probe path)
- Bind to an explicit address; do not assume public `0.0.0.0` without auth
- Token auth when the service is network-reachable (LanceDB pattern)

## Redistribution (who may pull, and how widely)

The workbench is open source, and images should be pullable widely — but "widely"
has a license boundary that the contract encodes in a `redistribution` field per
image (`public` | `restricted`), enforced by
`npa/tests/docker/test_packaging_contract.py`.

- **`public`** — OSS-redistributable. Code is under OSI-approved licenses
  (Apache-2.0 / BSD-3 / MIT / MPL-2.0), the CUDA/PyTorch base images
  (`nvidia/cuda`, `pytorch/pytorch` on Docker Hub) are freely redistributable,
  and any gated model weights (Cosmos, GR00T N1, Cosmos-Reason) are pulled at
  **runtime** by the operator, never baked into the image. These may be published
  to a public/anonymous registry.
- **`restricted`** — bakes a runtime we are not licensed to redistribute. Such an
  image may be built and run by the operator who owns the registry (internal R&D,
  build-your-own), but hosting it **prebuilt on a public/anonymous registry** would
  make us the third-party redistributor.

  **No image is currently `restricted`.** `isaac-lab`, `sonic`, `sonic-mujoco` and
  `groot` used to be, and the re-architecture that changed that is described below.
  The class and its guards are kept, with empty membership, for the next runtime we
  cannot ship.

## Runtime-fetched Isaac Sim (why the Isaac images are publishable)

The four Isaac images used to bake **NVIDIA Omniverse Kit (Isaac Sim)**. The Isaac Sim
*source* is Apache-2.0, but the shipped binary bundles the Kit SDK and NVIDIA assets,
and — this is the part that is easy to get wrong — **both** the `isaacsim` *and*
`isaaclab` PyPI packages declare `License: NVIDIA Proprietary Software`, with
`isaaclab/__init__.py` carrying an explicit no-redistribution header. The
`isaac-sim/IsaacLab` **GitHub repo** is BSD-3-Clause; the wheel is a
differently-licensed repackaging of it. So "Isaac Lab is BSD-3, we can bake that half"
is wrong, and read-the-metadata beats read-the-badge.

A runtime token could not have rescued a baked image: a token gates a *download*, and
Kit was already in the layers. So the images were changed to make the statement true.

**How it works.** The images contain **no NVIDIA Isaac bytes**. On first use of
`/isaac-sim/python.sh`, `npa/docker/workbench/common/isaac_bootstrap.sh`:

1. **Refuses** unless the operator has set **both** `OMNI_KIT_ACCEPT_EULA=YES` and
   `ISAACSIM_ACCEPT_EULA=YES`. Without them it exits 78 and downloads nothing. Nothing
   is baked with acceptance pre-granted — **this refusal is the legal mechanism**, and
   `npa/tests/docker/test_packaging_contract.py` fails the build if any image
   reintroduces a baked `*_ACCEPT_EULA`.
2. Installs the pinned `isaacsim`/`isaaclab` wheels from `https://pypi.nvidia.com` into
   a **cache volume**, not the image layers. Every wheel is pinned to a committed
   `sha256` and installed with `--no-deps --require-hashes` against `--index-url` (not
   `--extra-index-url`, so the set cannot be shadowed from PyPI).
3. Also fetches the **BSD-3 Isaac Lab source tree** at a pinned commit, because the
   wheel ships the library but no `scripts/`, and the SkyPilot Isaac tasks invoke
   `scripts/reinforcement_learning/rsl_rl/train.py`.
4. Is idempotent, concurrency-safe (`flock` + a version-stamped tree + atomic rename +
   `.complete` written last), and verifies the install before publishing it.

`pypi.nvidia.com` serves these wheels **anonymously**, so the credential was never the
gate — acceptance is. NVIDIA delivers Isaac to each operator under that operator's own
acceptance, and we redistribute nothing. This is the same pattern already used for gated
model weights (Cosmos, GR00T N1, Cosmos-Reason).

**What it costs.** Measured on an RTX PRO 6000 Blackwell node:

| | |
| --- | --- |
| cold start (~4.5 GB downloaded) | **111 s** |
| warm start | **32 ms** |
| cache volume per pinned version | **10.04 GiB** |
| 8 pods racing one cache | 114 s total; one installer, seven waiters, no corruption |

A per-pod `emptyDir` makes every pod pay that, and a node running 8 GPU pods downloads
~36 GB. Warm a **shared** volume once instead:

```bash
kubectl apply -f npa/docker/workbench/common/warm-isaac-cache.yaml
kubectl wait --for=condition=complete job/npa-warm-isaac-cache --timeout=30m
```

Then run workload pods against the same volume with `NPA_ISAAC_CACHE_READONLY=1`, so the
runtime user never needs write access to the cache at all. Other knobs:
`NPA_ISAAC_CACHE_DIR`, `NPA_ISAAC_INDEX_URL` (point it at an internal mirror; the wheels
are sha256-pinned, so a mirror is verifiable), `NPA_ISAAC_BOOTSTRAP_OFFLINE=1`.

**Why the published image is not "Isaac Sim with Omniverse Kit".** Because it does not
contain Isaac Sim. That is verified mechanically against the *built* image, not by
reading the Dockerfile:

```bash
npa/.venv/bin/python npa/scripts/scan_image_omniverse_payload.py \
    cr.<region>.nebius.cloud/<registry-id>/npa-isaac-lab:2.3.2.post1
```

The scanner streams the image's flattened filesystem and its layer history, matching Kit
payload signatures (`libcarb`, `kit/kernel/`, `omni.*` extension dirs, `extscache`,
`*.kit`, `site-packages/isaacsim`) against a short, explicit allowlist. It cannot simply
grep for "isaac": the images deliberately keep a `/isaac-sim/python.sh` **shim**, because
~30 call sites already invoke Isaac through that path and Kubernetes pods override
`ENTRYPOINT`, making the shim the only reliable bootstrap trigger. On
`npa-isaac-lab` it scans 83,043 entries and reports 21 allowlisted paths — the shim, the
bootstrap, the pinned manifests, two smoke scripts and two empty mount points — and
`VERDICT: clean`.

**Build-your-own still works, and no longer needs NGC credentials at all**, because there
is nothing credentialed left to pull:

```bash
npa/docker/workbench/isaac-lab/build.sh --registry cr.<region>.nebius.cloud/<id> --push
npa/docker/workbench/sonic/build.sh    --registry cr.<region>.nebius.cloud/<id> --push --variant baked
npa/docker/workbench/groot/build.sh    --registry cr.<region>.nebius.cloud/<id> --push
```

**The honest trade.** `npa-isaac-lab` went from 8.41 GB to 10.66 GB compressed (+27%).
Removing Isaac Sim saved less than adding a standalone PyTorch cu128 wheel set cost — its
bundled `nvidia-*` CUDA libraries are ~5 GB uncompressed, where the old nvcr.io base
shared its CUDA runtime with Kit's. Slimming the CUDA base from `-devel` to `-runtime` is
the obvious next lever.

Model weights are a separate axis and are never baked into any image: Cosmos,
GR00T N1, and Cosmos-Reason weights (and VLMs) are downloaded at **runtime**
using the customer's own HF/NGC token, so the customer accepts each model
license (e.g. the NVIDIA Open Model License) directly. We never redistribute
weights.

Access model today (both regions): each workbench registry
(`cr.eu-north1.nebius.cloud/…` primary, `cr.us-central1.nebius.cloud/…` mirror)
is **already readable org/tenant-wide** — the tenant `viewers`/`editors` groups
hold `viewer`/`editor`, which cascades to image pull — so developers inside the
owning org can pull every image, including the `restricted` ones (internal R&D
use).

**Pulling from any Nebius tenant / publicly.** Nebius Container Registry cannot
express "any Nebius tenant can pull": it has **no anonymous/public mode** and
**no `allAuthenticatedUsers` / cross-tenant grant**. Every pull needs an
authenticated identity, tenants are strictly isolated, and the only way to admit
an out-of-tenant identity is to invite that specific account into the owning
tenant and add it to a group — which does not scale to "anyone from any tenant."
The only way to make the images pullable by every Nebius tenant (which is also
pullable by anyone) is therefore to **mirror to a public-capable registry** —
GHCR (`ghcr.io`, the default), Docker Hub, or Quay.

Only the `public`-classified subset may be mirrored. Use the license-guarded
publisher, which copies exactly `publicly_publishable_tools()` (**19 images** — every
workbench image, now that the Isaac images fetch Isaac at run time) and hard-refuses
anything still classified `restricted`:

```bash
# defaults to $NPA_PUBLIC_REGISTRY, else ghcr.io/nebius/nebius-physical-ai
python -m npa.deploy.publish_public --dry-run
python -m npa.deploy.publish_public --target ghcr.io/<org>/<repo>
```

or the `Publish public images` GitHub Actions workflow (manual dispatch,
dry-run by default). **Consumers in any tenant** then pull the OSS images by
pointing the resolver at the public mirror:

```bash
export NPA_REGISTRY=ghcr.io/nebius/nebius-physical-ai   # OSS images, any tenant
```

Never add a `restricted` image to a public target. Nothing is currently classified
that way, and `publish_public` refuses anything that is, as defence in depth around the
selector.

> **Publishing is a business decision.** The engineering makes publication defensible —
> the images contain no NVIDIA-proprietary bytes, and NVIDIA delivers Isaac to each
> operator under that operator's own acceptance — but dispatching the workflow with
> `dry_run=false` should wait on sign-off from someone with the authority to accept it.

## Feature exposure

| Access mode | Contract |
| --- | --- |
| Container | ENTRYPOINT/CMD matches packaging tier; ports via `EXPOSE` |
| API | FastAPI endpoints: `/health`, `/status`, `/system-info`, `/list` (+ tool verbs) |
| CLI | `npa workbench <tool> ...` |
| SDK | `npa.sdk.workbench.<tool>` |
| YAML | `toolRef` in `catalog.py` + SkyPilot `image_id` from manifests |

Cross-tool data moves through S3 (`--input-path` / `--output-path`), never
direct service-to-service file coupling.

## Build and tag

1. Resolve registry with `npa.clients.config.resolve_container_registry`.
2. Build from the checked-in Dockerfile (`skills/atomic/build-and-push-image`).
3. Tag from `npa/pyproject.toml` `[tool.npa.supported-tools]` and
   `npa/docker/workbench/tags.yaml` (`cuda12` vs `cuda13-b300`).
4. SONIC variants: `npa/src/npa/deploy/sonic_image_manifest.json`.
5. Blackwell fleet digests: `npa/docker/workbench/sm120-images.json`.
6. Update golden evals when the image’s “does its job” command changes.

## Operator checklist (new or changed image)

- [ ] Dockerfile under `npa/docker/workbench/<tool>/`
- [ ] Packaging tier chosen and matches ENTRYPOINT
- [ ] Non-root final USER
- [ ] Base digest pinned or exception documented
- [ ] Registered in `CONTAINER_IMAGE_NAMES` + supported-tools version
- [ ] Golden eval entry present and passing offline validate
- [ ] CLI/SDK/YAML surfaces updated together
- [ ] Skill + `skills/index.yaml` smoke updated

## Related docs

- `docs/security/container-golden-evals.md` — usefulness + safety contract
- `docs/security/image-reproducibility.md` — digests and tag families
- `docs/architecture/oss-onboarding-ladder.md` — OSS → marketplace promotion
- `docs/workbench/cli-sdk-yaml-walkthrough.md` — three-access pattern
