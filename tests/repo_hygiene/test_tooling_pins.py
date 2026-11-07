import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = (ROOT / "Makefile").read_text()
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())


def _ci_gitleaks_version() -> str:
    versions = {
        step["env"]["GITLEAKS_VERSION"]
        for step in CI["jobs"]["secrets"]["steps"]
        if "GITLEAKS_VERSION" in step.get("env", {})
    }
    assert len(versions) == 1, versions
    return str(versions.pop())


def test_makefile_gitleaks_pin_is_the_ci_pin():
    match = re.search(r"^GITLEAKS_VERSION := (\S+)$", MAKEFILE, re.MULTILINE)
    assert match is not None, "Makefile must define GITLEAKS_VERSION once"
    assert match.group(1) == _ci_gitleaks_version() == "8.30.1"


def _fake_gitleaks(tmp_path: Path, version: str) -> dict[str, str]:
    exe = tmp_path / "gitleaks"
    exe.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = version ]; then echo "{version}"; exit 0; fi\n'
        'echo "fake gitleaks ran: $*"\n'
    )
    exe.chmod(0o755)
    return {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}


@pytest.mark.parametrize("version", ["8.0.0", "v8.30.0", ""])
def test_make_secrets_refuses_a_gitleaks_other_than_the_pin(tmp_path, version):
    env = _fake_gitleaks(tmp_path, version)
    proc = subprocess.run(
        ["make", "-s", "-C", str(ROOT), "secrets"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode != 0
    assert "8.30.1" in proc.stderr and "fake gitleaks ran" not in proc.stdout


def test_make_secrets_runs_gitleaks_when_the_pin_matches(tmp_path):
    env = _fake_gitleaks(tmp_path, "8.30.1")
    proc = subprocess.run(
        ["make", "-s", "-C", str(ROOT), "secrets"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "fake gitleaks ran: git --redact --no-banner" in proc.stdout


def test_dependabot_covers_every_pinned_ecosystem():
    cfg = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    ecosystems = {u["package-ecosystem"] for u in cfg["updates"]}
    assert {"uv", "github-actions", "docker-compose"} <= ecosystems
