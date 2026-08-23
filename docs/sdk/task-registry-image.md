# Task registry and Rerun image SDK

The SDK exposes the same ownership and immutable-byte checks as the CLI. Plan
first, then apply only after the operator has approved registry creation:

```python
from npa.sdk import registry
from npa.sdk.workbench import image

plan = registry.ensure_registry(
    project="<project-alias>",
    name="<unique-task-registry-name>",
    apply=False,
)
created = registry.ensure_registry(
    project="<project-alias>",
    name="<unique-task-registry-name>",
    apply=True,
)

built = image.build_rerun_viewer(
    project="<project-alias>",
    tag="<unique-validation-tag>",
    repo_root="<repository-root>",
)
checked = image.inspect_rerun_viewer(
    project="<project-alias>", tag="<unique-validation-tag>"
)
pushed = image.push_rerun_viewer(
    project="<project-alias>",
    tag="<unique-validation-tag>",
    expected_image_id=checked["image_id"],
    inspection_digest=checked["inspection_digest"],
)
verified = image.verify_rerun_viewer(
    project="<project-alias>",
    tag="<unique-validation-tag>",
    expected_digest=pushed["digest"],
)
```

`ensure_registry` refuses projects without durable NPA creation proof and never
selects a same-named registry outside the exact configured project. The image
SDK always uses that verified registry and the checked-in Rerun viewer
Dockerfile. Push is bound to the preceding local image/config digest and
capability inspection; verification pulls the immutable pushed digest and
repeats the probe. Token values are passed only on Docker login stdin.
