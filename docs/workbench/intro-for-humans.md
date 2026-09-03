# Nebius Physical AI Workbench — An Introduction for Humans

Nebius Physical AI Workbench (`npa`) is a CLI, SDK, and workflow layer for
running robotics, simulation, perception, and synthetic-data workloads on
Nebius infrastructure. It composes containerized tools — dataset curation,
Isaac Lab simulation, Cosmos generative augmentation, policy training,
evaluation, 3D reconstruction, teleoperation — through declarative
`npa.workflow` YAML specs and Nebius object storage, Kubernetes, and GPU
clusters (H100, H200, L40S, B300, RTX PRO 6000).

## It's built for agents first — and that's not a compromise for humans

Workbench is designed to be **operated by a coding agent** (Claude Code, or
similar) sitting in this checkout with terminal access, not driven command-by-
command by a human typing into a shell. That shapes almost everything about
how it feels to use:

- The repo ships **skills** (`skills/index.yaml`) — structured playbooks an
  agent reads to know exactly which command to run, in what order, for a given
  task. A human could read the same files, but they're written for an agent to
  execute, not for a human to skim.
- Nearly every command has a **preflight** check that fails closed with an
  exact, actionable error before spending real GPU time — designed so an agent
  can self-correct in a loop rather than a human debugging by hand.
- Errors are written to be machine-actionable ("run `X` before `Y`") more than
  narratively explained.

**As a human, you don't lose access to anything — you just don't need to be
the one typing `npa` commands.** Your job is to hold the credentials, make the
decisions only a human can make (accept a license, approve spend, judge a
result), and describe what you want in plain language. The agent does the
CLI work, reads the skills, and reports back.

You *can* run every `npa` command directly yourself — it's a real CLI, fully
documented in [`docs/cli/`](../cli/README.md). Most people don't, because the
agent loop is faster and the skills exist specifically to make an agent good
at this. Think of `npa` the way you'd think of a build system a CI runner
drives: usable by hand, designed for automation.

## Getting started

**1. Install.** Python 3.10+, install `npa` editable from the clone:

```bash
git clone https://github.com/nebius/nebius-physical-ai.git
cd nebius-physical-ai
python3 -m venv .venv && source .venv/bin/activate
pip install -e npa
npa --version
```

**2. Connect to Nebius.** Sign up, create a tenant/project, install the
Nebius CLI, then let `npa configure` write your credential and config files.
If an agent is operating the checkout, put tokens (`HF_TOKEN`, `NGC_API_KEY`,
`NEBIUS_TOKEN_FACTORY_KEY`, `NEBIUS_TENANT_ID`, `NEBIUS_PROJECT_ID`,
`NEBIUS_REGION`) in its **private process environment**, not in chat. See
[getting-started.md](getting-started.md) for the full walkthrough.

**3. Hand it to your agent.** Open this checkout in Claude Code (or your
agent of choice) and say:

> Set up Nebius Physical AI Workbench in this checkout. Read `AGENTS.md` and
> `skills/index.yaml` first. Use my environment credentials, run
> `npa configure`, then `npa workbench health preflight` and
> `npa workbench health access --capability sim2real,paidf,cosmos3`. Don't
> provision GPU resources yet — tell me what's missing first.

That last instruction matters: the platform is built so an agent checks
*everything* — tokens, gated model licenses, storage, cluster identity — before
it spends a dollar of GPU time. Let it run that gate before saying "go."

## What you can actually ask your agent to do

Once setup passes, here's the menu — roughly cheapest/fastest to most
involved. You don't need to know the underlying commands; these are things
you can just ask for.

| What you want | What it does | Ask your agent |
| --- | --- | --- |
| **A cheap first proof, no GPU** | Hosted inference through Nebius Token Factory — captioning, batch generation, Cosmos reasoning — on someone else's GPU, billed per token | *"Give me a zero-GPU proof this is all wired up — caption an image through Token Factory."* |
| **Prove a container actually works** | Runs a per-image hello-world manifest against dry-run, local, or serverless tiers before you trust it on real hardware | *"Run golden-eval on the Isaac Lab image and show me it passes."* |
| **Turn a video into synthetic training data** | The Physical AI Data Factory: Cosmos Transfer augments a source clip, a real Cosmos Evaluator gates quality, FiftyOne curates the result | *"Run the PAIDF Cosmos 3 workflow on this video and show me the augmented output in Rerun."* |
| **Train and evaluate a real robot policy end to end** | The flagship Sim2Real workflow: trigger → Cosmos augmentation → parallel environment generation → Isaac Lab rollouts → hosted VLM scoring → PPO training → strict gold evaluation → visualization. 14 logical stages, real GPU compute throughout | *"Submit the sim2real workflow against my cluster with the public Franka-lift preset and walk me through the result."* |
| **Curate production sensor data** | Ingests, validates, and versions real sensor logs as a lineage-tracked dataset you can query later | *"Ingest this run's sensor data as a dataset and show me what's in it."* |
| **Find where a policy breaks** | An RL adversary that searches for scenarios that maximize failures of a policy under test, then ranks them | *"Find the scenarios where my current policy fails most often."* |
| **Reconstruct a real scene in 3D** | Captures real-world footage into a NeRF/3D-Gaussian scene you can render novel views from and drop into simulation | *"Turn this capture into a renderable scene and show me a novel view."* |
| **Teleoperate a robot from a browser** | Browser-based teleoperation producing immutable LeRobot-format datasets | *"Set up a LeIsaac teleop session and record a demonstration."* |
| **Stand up GPU clusters at scale** | Deploys one or many Nebius Managed Kubernetes clusters from a declarative fleet spec, across projects if needed | *"Deploy a 3-cluster fleet for the team's RTX PRO 6000 workloads."* |

Every one of these has a **preflight** gate: credentials, gated-model access,
cluster capacity, image pullability. If something's missing, the agent will
tell you exactly what (often a specific Hugging Face model page to accept)
rather than launching and failing halfway through.

## A worked example, briefly

A recent live run looked like this: ask the agent to run the Sim2Real
workflow against a real 2-GPU cluster → it checks credentials and gated-model
access, builds the five required container images in-cluster (no Docker
needed locally), registers the cluster with the platform, submits the
workflow, and drives it through all 14 stages — Cosmos augmentation,
parallel environment generation, Isaac Lab rollouts, hosted evaluation, PPO
training, strict gold evaluation — to a final report, a shareable Rerun
recording, and an MCAP file. When something failed partway (a licensing gap
on a model dependency), the agent found the real cause, got it accepted, and
resumed the run from where it stopped rather than starting over. That
loop — describe the goal, let the agent run the gates and the workflow, review
the result — is the normal way to use this platform.

## Where to go next

- [`docs/quickstart.md`](../quickstart.md) — the full setup walkthrough,
  including the flagship NVIDIA Cosmos path.
- [`docs/workbench/guides/`](guides/README.md) — one guide per major
  workflow, written as operator runbooks.
- [`docs/cli/README.md`](../cli/README.md) — full command reference, if you
  want to drive `npa` yourself.
- `skills/index.yaml` — the map your agent reads; skim it if you're curious
  what it's about to do.
- When in doubt, just ask your agent "what can I run next?" — the platform is
  built to answer that question cheaply, before anything expensive happens.
