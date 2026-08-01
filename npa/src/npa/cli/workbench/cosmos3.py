"""Workbench Cosmos3 commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from npa.workbench.cosmos.text_to_image import DEFAULT_UV_GROUP
from npa.workflows.cosmos_split import (
    Cosmos3ReasonConfig,
    build_cosmos3_reason_manifest,
    write_manifest,
)

app = typer.Typer(
    name="cosmos3",
    help="Cosmos3 reasoning workflow contracts.",
    no_args_is_help=True,
)


@app.command("reason")
def reason_cmd(
    input_uri: str = typer.Option(..., "--input-uri", help="Input rollout or frame URI."),
    output_uri: str = typer.Option(..., "--output-uri", help="Output prefix for reasoning JSON."),
    model: str = typer.Option("nvidia/Cosmos-Reason1-7B", "--model", help="Reasoning model id."),
    image: str = typer.Option("", "--image", help="BYO Cosmos3 reason image."),
    prompt: str = typer.Option("", "--prompt", help="Optional reasoning prompt."),
    run_id: str = typer.Option("", "--run-id", help="Run id carried into the manifest."),
    output_json: Optional[Path] = typer.Option(None, "--output-json", help="Write manifest JSON locally."),
) -> None:
    """Build the Cosmos3 reason stage manifest."""

    payload = build_cosmos3_reason_manifest(
        Cosmos3ReasonConfig(
            input_uri=input_uri,
            output_uri=output_uri,
            model=model,
            image=image,
            prompt=prompt,
            run_id=run_id,
        )
    )
    if output_json is not None:
        payload = write_manifest(payload, output_json)
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("text-to-image")
def text_to_image_cmd(
    prompt: str = typer.Option(..., "--prompt", help="Text prompt to generate an image from."),
    output_uri: str = typer.Option(
        "", "--output-uri", help="S3 prefix to publish the image and its manifest to."
    ),
    output_dir: Path = typer.Option(
        Path("/tmp/npa-cosmos3-inference"),
        "--output-dir",
        help="Local working directory for inference outputs.",
    ),
    model_id: str = typer.Option("", "--model-id", help="HF model repo id for the checkpoint."),
    checkpoint_name: str = typer.Option(
        "Cosmos3-Nano",
        "--checkpoint-name",
        help="Checkpoint name the framework's inference entrypoint expects.",
    ),
    source_repo_url: str = typer.Option(
        "", "--source-repo-url", help="Cosmos framework source repository URL."
    ),
    cache_dir: Optional[Path] = typer.Option(
        None, "--cache-dir", help="Ephemeral runtime cache for source and checkpoint."
    ),
    uv_group: str = typer.Option(
        DEFAULT_UV_GROUP, "--uv-group", help="uv dependency group to sync in the framework repo."
    ),
    seed: int = typer.Option(0, "--seed", help="Inference seed."),
    guardrails: bool = typer.Option(
        False,
        "--guardrails/--no-guardrails",
        help="Run the framework's content guardrails (they download extra gated weights).",
    ),
    hf_token_env: str = typer.Option(
        "HF_TOKEN", "--hf-token-env", help="Environment variable holding the Hugging Face token."
    ),
    github_token_env: str = typer.Option(
        "GITHUB_TOKEN", "--github-token-env", help="Environment variable holding a GitHub token."
    ),
) -> None:
    """Generate an image from a prompt with the Cosmos3 framework, and publish it.

    Replaces `skypilot/cosmos3-text-to-image-inference.yaml`, which carried the whole procedure
    as bash inside an `envs:` block — unreachable from the CLI or the SDK, and untestable.
    """

    from npa.workbench.cosmos.cosmos3 import Cosmos3AccessConfig
    from npa.workbench.cosmos.text_to_image import Cosmos3TextToImageError, generate

    config = Cosmos3AccessConfig.from_env(
        model_id=model_id,
        source_repo_url=source_repo_url,
        cache_dir=cache_dir,
        github_token_env=github_token_env,
        hf_token_env=hf_token_env,
    )
    try:
        result = generate(
            config,
            prompt=prompt,
            output_dir=output_dir,
            seed=seed,
            guardrails=guardrails,
            uv_group=uv_group,
            checkpoint_name=checkpoint_name,
            publish_uri=output_uri,
        )
    except Cosmos3TextToImageError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result.as_dict(), indent=2, sort_keys=True))
