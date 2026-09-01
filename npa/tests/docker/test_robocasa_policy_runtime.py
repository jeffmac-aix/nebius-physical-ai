"""Static contract for the combined RoboCasa + LeRobot ACT evaluation runtime."""

from pathlib import Path


DOCKERFILE = (
    Path(__file__).resolve().parents[2]
    / "docker"
    / "workbench"
    / "robocasa"
    / "Dockerfile"
)


def test_robocasa_keeps_known_good_gymnasium_and_policy_only_lerobot() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert '"gymnasium==0.29.1"' in text
    assert 'pip install --no-cache-dir --no-deps "lerobot==0.5.1"' in text
    assert '"draccus==0.10.0"' in text
    assert '"einops>=0.8.0,<0.9.0"' in text
