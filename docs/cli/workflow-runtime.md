# `npa agent workflow-runtime`

## Command Tree

```text
Usage: npa agent workflow-runtime [OPTIONS] COMMAND [ARGS]...

Prepare, inspect, and stop an isolated NPA workflow runtime.

Options
--help  Show this message and exit.
Commands
prepare  Prepare an isolated runtime and verify its exact workflow target.
status  Inspect one exact workflow runtime without changing it.
stop  Stop only the isolated workflow runtime in this owner scope.
```

## Options

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## Subcommands

| Command | Description |
| --- | --- |
| `prepare` | Prepare an isolated runtime and verify its exact workflow target. |
| `status` | Inspect one exact workflow runtime without changing it. |
| `stop` | Stop only the isolated workflow runtime in this owner scope. |

## Examples

```bash
npa agent workflow-runtime --help
npa agent workflow-runtime prepare --help
```

Regenerate this page with `bash scripts/build_docs.sh` after changing `workflow-runtime`.
