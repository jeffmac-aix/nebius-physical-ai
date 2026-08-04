# `npa workbench leisaac`

## Command Tree

```text
Usage: npa workbench leisaac [OPTIONS] COMMAND [ARGS]...

LeIsaac SO101 browser teleoperation on an RT-core Kubernetes GPU.

Options
--help  Show this message and exit.
Commands
launch  Launch PickOrange with upstream keyboard teleoperation and publish its UI capability.
status  Report the live Kubernetes objects for a LeIsaac run.
destroy  Delete this run's transient GPU deployment and LBs, preserving S3 evidence.
```

## Options

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## Subcommands

| Command | Description |
| --- | --- |
| `launch` | Launch PickOrange with upstream keyboard teleoperation and publish its UI capability. |
| `status` | Report the live Kubernetes objects for a LeIsaac run. |
| `destroy` | Delete this run's transient GPU deployment and LBs, preserving S3 evidence. |

## Examples

```bash
npa workbench leisaac --help
npa workbench leisaac launch --help
```

Regenerate this page with `bash scripts/build_docs.sh` after changing `leisaac`.
