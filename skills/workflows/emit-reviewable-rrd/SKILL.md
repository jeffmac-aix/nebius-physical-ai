---
name: emit-reviewable-rrd
description: Use when authoring, reviewing, or operating an NPA workflow whose real run outputs should become a reviewable Rerun .rrd recording with factual timelines, provenance, declared artifacts, and independent content validation.
---

# Emit reviewable RRD artifacts

Build the recording from the run being reviewed. A stock example, generated
placeholder, screenshot, or renamed JSON file is never evidence for the run.

## Procedure

1. Identify the actual stage outputs that contain the facts to visualize. Keep
   raw customer inputs private; extract only the metrics, events, frames, or
   trajectories needed for review.
2. Choose timelines that preserve source semantics. Use `optimizer_step` for
   training progress, real capture time for timestamped sensors, and an
   explicitly labelled dataset/frame index when capture time does not exist.
3. Choose stable entity paths before writing. For training, prefer grouped
   entities such as `metrics/loss`, `metrics/learning_rate`,
   `throughput/global_samples_per_second`, `health/gradient_norm`,
   `checkpoint/materialized`, and `provenance/run`.
4. Set the Rerun recording id to the workflow run id. Record sanitized static
   provenance: producer, source revision, recipe/config identity, source
   artifact hashes, and factual limitations. Never embed credentials, signed
   URLs, customer payloads, hostnames, pod/node ids, or private infrastructure
   identifiers.
5. Write the recording with `rerun-sdk` and close its sink before inspection.
   Reuse an existing NPA Rerun converter or inspection helper when it matches
   the source; extend the producing workbench integration when it does not.
6. Put the file at a run-scoped private URI such as
   `s3://<bucket>/<workflow>/<run.id>/reports/<name>.rrd`. Declare that exact URI
   in the producing state's `outputs` with schema
   `application/vnd.rerun.rrd`. Keep inputs and all companion artifacts under
   the same run prefix so `npa workbench workflow artifacts` and artifact-first
   discovery can find them.
7. Fail the artifact stage if the required recording cannot be created,
   uploaded, or validated. Do not turn a mandatory RRD into a warning-only
   side effect.

## Make the content reviewable

- For optimization, log factual loss, the exact applied learning-rate schedule,
  interval timing/throughput, finite gradient or update-health diagnostics,
  checkpoint events, and aggregate distributed/device health on the
  `optimizer_step` timeline.
- Include before/after or held-out policy trajectories only when this run
  actually produced both sides with a valid alignment. Otherwise state the
  limitation in provenance and omit those entities.
- Use a blueprint when it materially improves the first view, but keep the
  underlying entities independently inspectable.
- Prefer a durable, deduplicated metric journal during long jobs and convert it
  deterministically after success. This makes resume factual without relying on
  unsupported append/recovery behavior for a partial RRD.

## Validate before handoff

Independently validate the uploaded bytes:

- run `rerun rrd verify <file>`;
- inspect with `rerun rrd print -vv <file>`;
- require the expected application id, run recording id, timelines, and entity
  paths in decoded output;
- compare decoded coverage with the source journal/manifest;
- verify non-empty S3 bytes by read-after-write; and
- confirm artifact discovery lists the exact run-scoped `.rrd`.

An extension, a viewer opening, or a producer's own success message is not
enough. Preserve the inspection result and content hash in the run report.

## Creation versus sharing

Creating and privately storing the recording is the workflow contract. Sharing
is optional and separate. Use `npa rerun host` or `npa rerun share` only when
the operator asks for a time-boxed presigned link; follow
`skills/tools/artifact-viz-share/SKILL.md` and treat the link as a credential.

## Verify repository changes

Run the relevant workflow/output tests, then:

```bash
npa/.venv/bin/python -m pytest npa/tests/guardrails/test_skills_index.py -q
npa/.venv/bin/python /home/ubuntu/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/workflows/emit-reviewable-rrd
```
