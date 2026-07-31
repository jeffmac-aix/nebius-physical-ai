---
name: solution-licensing
description: Use when adding, onboarding, or repackaging any solution, tool, container image, model, or dataset, to determine whether what we ship may be redistributed, and to record that decision where the build enforces it.
---

# Solution Licensing And Redistribution

Use this skill whenever work adds something new to what NPA ships: a workbench
tool, a BYOF/OSS solution, a container image, a base-image swap, a model, or a
dataset. It answers one question:

> We are about to bundle someone else's software. **Who may we hand the result
> to, and in what form?**

"It's open source" does not answer that. The whole point of this skill is that
the answer usually depends on components the top-level license does not cover.

This is engineering guidance for classifying and recording a decision, not legal
advice. When a license is novel, ambiguous, or the vendor's own sources
disagree, record the finding and escalate to a human rather than picking the
convenient reading.

## When To Use

- Onboarding an OSS or partner solution (pairs with
  `skills/workflows/oss-solution-registry-onboard/SKILL.md`, whose License
  admission gate this skill implements)
- Adding a workbench image or changing a Dockerfile's `FROM`
- Adding a model, checkpoint, or dataset to a workflow
- Deciding whether something may be published to a public registry
- Reviewing a PR that does any of the above

## The Three Layers

Classify each layer separately. A permissive answer at one layer says nothing
about the others, and it is almost always a lower layer that constrains us.

| Layer | What it is | Typical trap |
| --- | --- | --- |
| **Source** | The project's own code | Apache/MIT badge on a repo whose *build* pulls proprietary parts |
| **Baked runtime** | Everything the image carries: base image, wheels, SDKs, binaries, assets, fonts, textures | Free to *use*, not to *redistribute* |
| **Weights and data** | Model checkpoints, datasets, assets fetched or baked | Gated licenses with field-of-use or non-commercial terms |

The decisive layer is normally the **baked runtime**, because publishing an image
distributes every byte in it to whoever pulls it.

## Procedure

### 1. Enumerate what the artifact actually ships

Do not read the README. Read the Dockerfile and the lockfile.

```bash
grep -nE '^(FROM|ARG .*(BASE|IMAGE|VERSION))' npa/docker/workbench/<tool>/Dockerfile
grep -nE 'pip install|apt-get install|curl|wget|COPY --from' npa/docker/workbench/<tool>/Dockerfile
```

List: base image, every SDK/wheel installed from a vendor index (for example
`pypi.nvidia.com`, `nvcr.io`), anything downloaded at build time, and any baked
weights or assets.

### 2. Find each component's real license

Prefer the vendor's own licensing page or the package's own metadata over a
repo badge or a summary. Useful checks:

```bash
pip download --no-deps --no-binary :all: <pkg>   # then read the sdist metadata
python -c "import importlib.metadata as m; print(m.metadata('<pkg>')['License'])"
```

A package whose `License` field literally reads *"NVIDIA Proprietary Software"*
settles the question regardless of what the GitHub repo's badge says.

### 3. Ask the redistribution question explicitly

For every component, answer these four separately — permission for one is not
permission for another:

1. May we **use** it internally?
2. May we **redistribute** it to third parties (shipping an image counts)?
3. May we **run it as a service** for third parties? Vendors often treat
   "install and operate it for a customer" as redistribution even though no
   bits change hands.
4. Are there **field-of-use limits** (non-commercial, research-only, no
   competing service, evaluation-only)?

### 4. Resolve conflicting vendor sources

Vendors publish general terms and product-specific terms, and they diverge.
When they do, prefer the source that is **more specific to the component we
actually ship**, and then the **more recent**. Record which source you relied
on and its date, because the next person will find the other one and assume we
were wrong.

### 5. Record the decision where the build enforces it

A conclusion in a PR description is not a control. Encode it:

- `npa/docker/workbench/packaging-contract.yaml` — set `redistribution: public`
  or `restricted` on the image entry.
- `npa/src/npa/deploy/images.py` — add restricted tools to
  `OMNIVERSE_RESTRICTED_TOOLS` (or the equivalent set) so
  `publicly_publishable_tools()` and `publish_public` exclude them, and so
  resolving them from a public registry fails loudly.
- For a solution's weights/datasets, record the license and the runtime-fetch
  requirement in the capability table from the onboarding skill.

The packaging-contract guards then fail the build if a Dockerfile bakes a
restricted marker, or is built `FROM` a restricted image, while claiming
`public`.

## Patterns That Keep Us Compliant

Two patterns do the real work. Prefer them over asking for an exception.

**Runtime fetch under the customer's own credentials.** Never bake gated weights.
The image ships the *code* to download them; the operator supplies their own
HF/NGC token at run time and accepts the model license directly. We never
redistribute weights, and the customer's entitlement is theirs. This is how
Cosmos, GR00T N1, and Cosmos-Reason weights are handled.

**Build-your-own.** For a runtime we may not redistribute, ship the Dockerfile
and the build tooling, not the built image. Each operator builds into their own
registry (`build.sh --registry <their-registry> --push`), pulling the vendor
base with their own credentials and EULA acceptance. The vendor delivers to
each operator under that operator's own acceptance; we ship only instructions.

Both patterns share one idea: **move the vendor's delivery to the customer**, so
we are never the redistributor.

## Worked Precedent: Isaac Sim / Omniverse Kit

The canonical case in this repo, and a good template for reasoning.

- **Source:** Isaac Sim's GitHub source is Apache-2.0 — freely redistributable.
- **Baked runtime:** building or running it requires NVIDIA-owned components
  (Omniverse Kit SDK, 3D models, textures) under the *NVIDIA Isaac Sim
  Additional Software and Materials License*. Those **may not be redistributed**;
  redistributing Isaac Sim with Omniverse Kit to third parties, or delivering it
  as a service to third parties, requires an NVIDIA AI Enterprise license.
- **Verdict:** internal R&D is free with no seat limit, so our own developers
  pulling these images from our own registry is fine. Publishing them to a public
  registry, or handing a prebuilt image to a customer, is not. Hence
  `isaac-lab`, `sonic`, `sonic-mujoco`, and `groot` are `restricted`, and
  customers use build-your-own.
- **Useful carve-outs:** selling simulation *outputs* (datasets, videos,
  reports), or selling custom code and USD assets that the customer runs on
  *their own* Isaac Sim, do not require a license. Our data-generation and
  policy-training products sit inside these carve-outs.

**The trap.** As of May 2026 NVIDIA announced that Omniverse is free for
development, production, *and redistribution*. Read alone, that looks like it
lifts the restriction. It does not: the Isaac Sim Additional Software and
Materials License is the product-specific license for what these images
actually bake, and the Isaac Sim 6.0 documentation — GA'd 4 June 2026, i.e.
*after* that announcement — still states that third-party redistribution
requires NVIDIA AI Enterprise. More specific and more recent wins. Do not
reclassify these images on the strength of the general Omniverse page; that
needs NVIDIA confirmation in writing.

## Red Flags

Any of these means stop and classify carefully rather than assuming OSS:

- A build step authenticates to a vendor registry or index (`nvcr.io`,
  `pypi.nvidia.com`, a login wall, an API key)
- The Dockerfile sets an EULA acceptance variable (`*_ACCEPT_EULA`,
  `ACCEPT_EULA`, `PRIVACY_CONSENT`) — something in there has terms attached
- The package's own metadata says proprietary, even on an OSS-branded project
- The license permits "use" or "internal use" but is silent on distribution;
  silence is not permission
- `FROM` points at another restricted image — restriction is inherited, and the
  child Dockerfile will show no marker of its own
- Weights or assets are downloaded during `docker build` rather than at run time
- The license names an entity type we might be (cloud provider, service
  provider, competitor) in a carve-out

## Verify

```bash
npa/.venv/bin/python -m pytest npa/tests/docker/test_packaging_contract.py -q
npa/.venv/bin/python -m pytest npa/tests/deploy/ -q
npa/.venv/bin/python -m pytest npa/tests/guardrails/test_skills_index.py -q
npa/.venv/bin/python -m npa.deploy.publish_public --dry-run
```

The publish dry run is the end-to-end check: whatever it lists is what we would
hand to the public, so a restricted image appearing there is a hard stop.

## Gotchas

- "Free" means free of charge, not free to redistribute. They are unrelated
  questions and vendors price them separately.
- Running a workload for a customer can be redistribution in license terms even
  though no artifact is delivered. Check layer 3 whenever we host something.
- An image is not a bag of licenses; it is a single artifact you hand over
  whole. The most restrictive component governs the whole image.
- Access control is not a license. Making a registry private limits who *can*
  pull, but if a third party pulls a prebuilt restricted image with our
  blessing, that is still redistribution.
- Deleting a public tag does not undo publication. Treat a mistaken publish as
  an incident, not a revert.
- Record the license decision at onboarding time. Reconstructing why an image
  was classified two years later, after the vendor's page has changed, is far
  harder than writing one paragraph now.
