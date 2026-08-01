"""Unit tests for `npa.workbench.cosmos.text_to_image`.

The retired template could not be tested at all: its inference procedure lived in a multi-line
environment variable. As a module, the parts that do not need an H100 are checkable — the job
document, the argv, and above all the verification, which is what stands between "exit 0" and
"a real image".
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest

from npa.workbench.cosmos import text_to_image as t2i


def _png(width: int, height: int, *, pad: int = 4096) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00" * pad))
        + chunk(b"IEND", b"")
    )


def _jpeg(width: int, height: int, *, pad: int = 4096) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x00" * 9
    return b"\xff\xd8" + sof + b"\xff\xdb" + struct.pack(">H", 2 + pad) + b"\x00" * pad + b"\xff\xd9"


def test_reads_png_dimensions(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(_png(1024, 576))
    assert t2i.image_dimensions(path) == (1024, 576)


def test_reads_jpeg_dimensions(tmp_path: Path) -> None:
    path = tmp_path / "a.jpg"
    path.write_bytes(_jpeg(1280, 720))
    assert t2i.image_dimensions(path) == (1280, 720)


def test_verify_accepts_a_real_image(tmp_path: Path) -> None:
    path = tmp_path / "ok.png"
    path.write_bytes(_png(512, 512))
    size, width, height = t2i.verify_image(path)
    assert (width, height) == (512, 512)
    assert size > t2i.MIN_IMAGE_BYTES


def test_verify_rejects_a_missing_image(tmp_path: Path) -> None:
    with pytest.raises(t2i.Cosmos3TextToImageError, match="produced no image"):
        t2i.verify_image(tmp_path / "nope.png")


def test_verify_rejects_a_truncated_image(tmp_path: Path) -> None:
    """The failure worth catching: the framework exits 0 having written nothing useful."""

    path = tmp_path / "tiny.png"
    path.write_bytes(_png(8, 8, pad=1))
    with pytest.raises(t2i.Cosmos3TextToImageError, match="expected at least"):
        t2i.verify_image(path)


def test_verify_rejects_a_file_that_is_not_an_image(tmp_path: Path) -> None:
    path = tmp_path / "log.txt"
    path.write_bytes(b"traceback follows\n" * 500)
    with pytest.raises(t2i.Cosmos3TextToImageError, match="unrecognised image format"):
        t2i.verify_image(path)


def test_job_document_matches_the_framework_contract() -> None:
    assert t2i.build_job_document("a robot") == {
        "model_mode": "text2image",
        "name": "npa-t2i",
        "prompt": "a robot",
    }


def test_inference_argv_is_an_argv_not_a_string(tmp_path: Path) -> None:
    argv = t2i.inference_argv(
        input_json=tmp_path / "in.json",
        output_dir=tmp_path / "out",
        checkpoint_name="Cosmos3-Nano",
        seed=7,
        guardrails=False,
    )

    assert argv[:3] == [".venv/bin/python", "-m", "cosmos_framework.scripts.inference"]
    assert "--no-guardrails" in argv
    assert "--seed=7" in argv
    assert argv[argv.index("--checkpoint-path") + 1] == "Cosmos3-Nano"
    # No shell interpolation anywhere: every element is a discrete token.
    assert all(" " not in part or part.startswith("/") or "tmp" in part for part in argv)


def test_guardrails_flag_is_opt_in(tmp_path: Path) -> None:
    argv = t2i.inference_argv(
        input_json=tmp_path / "in.json",
        output_dir=tmp_path / "out",
        checkpoint_name="Cosmos3-Nano",
        seed=0,
        guardrails=True,
    )
    assert "--no-guardrails" not in argv


def test_publish_writes_the_manifest_and_uploads_both(tmp_path: Path, monkeypatch) -> None:
    uploads: list[tuple[str, str]] = []

    class _Client:
        @staticmethod
        def from_environment() -> "_Client":
            return _Client()

        def upload_file(self, local: str, uri: str) -> str:
            uploads.append((Path(local).name, uri))
            return uri

    monkeypatch.setitem(
        __import__("sys").modules,
        "npa.clients.storage",
        type("m", (), {"StorageClient": _Client}),
    )

    image = tmp_path / t2i.IMAGE_FILENAME
    image.write_bytes(_png(64, 64))
    result = t2i.TextToImageResult(
        status="ok",
        prompt="a robot",
        model_id="nvidia/Cosmos3-Nano",
        output_image=str(image),
        bytes=image.stat().st_size,
        width=64,
        height=64,
        seed=0,
        source_dir="/tmp/src",
        checkpoint_dir="/tmp/ckpt",
    )

    published = t2i.publish(result, tmp_path, "s3://bucket/run")

    assert published["image_uri"] == "s3://bucket/run/" + t2i.IMAGE_FILENAME
    assert published["manifest_uri"] == "s3://bucket/run/" + t2i.MANIFEST_FILENAME
    manifest = json.loads((tmp_path / t2i.MANIFEST_FILENAME).read_text())
    assert manifest["schema"] == "npa.cosmos3.text_to_image.v1"
    assert manifest["prompt"] == "a robot"
    assert sorted(name for name, _ in uploads) == sorted(
        [t2i.IMAGE_FILENAME, t2i.MANIFEST_FILENAME]
    )


def test_generate_requires_a_prompt(tmp_path: Path) -> None:
    from npa.workbench.cosmos.cosmos3 import Cosmos3AccessConfig

    with pytest.raises(t2i.Cosmos3TextToImageError, match="prompt is required"):
        t2i.generate(
            Cosmos3AccessConfig.from_env(environ={}),
            prompt="   ",
            output_dir=tmp_path,
        )


def test_the_spec_declares_the_manifest_the_tool_writes() -> None:
    import yaml

    spec_path = (
        Path(__file__).resolve().parents[3]
        / "npa"
        / "workflows"
        / "workbench"
        / "npa-workflows"
        / "cosmos3-text-to-image.yaml"
    )
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    declared = spec["states"]["text-to-image"]["outputs"][0]["uri"]

    assert declared.endswith(t2i.MANIFEST_FILENAME)
