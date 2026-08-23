# `npa workbench image`

## Command Tree

```text
Usage: npa workbench image [OPTIONS] COMMAND [ARGS]...

Build and verify task-owned workbench images.

Options
--help  Show this message and exit.
Commands
build-rerun-viewer  Build the checked-in Rerun viewer into the local Docker engine.
inspect-rerun-viewer  Inspect and capability-probe the exact local Rerun image bytes.
push-rerun-viewer  Push only a prior digest-bound compatible local Rerun image.
verify-rerun-viewer  Pull and probe the exact digest that will be supplied to workflow preflight.
```

## Options

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## Subcommands

| Command | Description |
| --- | --- |
| `build-rerun-viewer` | Build the checked-in Rerun viewer into the local Docker engine. |
| `inspect-rerun-viewer` | Inspect and capability-probe the exact local Rerun image bytes. |
| `push-rerun-viewer` | Push only a prior digest-bound compatible local Rerun image. |
| `verify-rerun-viewer` | Pull and probe the exact digest that will be supplied to workflow preflight. |

## Examples

```bash
npa workbench image --help
npa workbench image build-rerun-viewer --help
```

Regenerate this page with `bash scripts/build_docs.sh` after changing `image`.
