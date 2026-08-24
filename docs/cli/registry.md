# `npa workbench registry`

## Command Tree

```text
Usage: npa workbench registry [OPTIONS] COMMAND [ARGS]...

Ensure and tear down exact registries in NPA-created projects.

Options
--help  Show this message and exit.
Commands
ensure  Plan or ensure a registry only inside an NPA-created project.
delete  Delete one exact registry only with durable NPA project-creation proof.
```

## Options

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## Subcommands

| Command | Description |
| --- | --- |
| `ensure` | Plan or ensure a registry only inside an NPA-created project. |
| `delete` | Delete one exact registry only with durable NPA project-creation proof. |

## Examples

```bash
npa workbench registry --help
npa workbench registry ensure --help
```

Regenerate this page with `bash scripts/build_docs.sh` after changing `registry`.
