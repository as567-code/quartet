import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _check_ignore(path: str) -> int:
    return subprocess.run(
        ["git", "check-ignore", "-q", path],
        check=False,
        cwd=REPO_ROOT,
    ).returncode


@pytest.mark.parametrize(
    "path",
    [".env", ".env.local", ".env.production", ".quartet/x", "eval/results/raw/run.json"],
)
def test_sensitive_paths_are_ignored(path: str) -> None:
    assert _check_ignore(path) == 0


def test_env_example_is_not_ignored() -> None:
    assert _check_ignore(".env.example") == 1
