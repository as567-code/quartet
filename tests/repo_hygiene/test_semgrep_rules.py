import re
import shlex
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _semgrep_cmd() -> list[str]:
    """The pinned `uvx semgrep@<version>` command, single-sourced from the Makefile."""
    m = re.search(r"^SEMGREP\s*:=\s*(.+)$", (ROOT / "Makefile").read_text(), re.MULTILINE)
    assert m, "Makefile must define SEMGREP"
    return shlex.split(m.group(1))


def test_repo_rules_catch_bad_fixtures(tmp_path: Path) -> None:
    # Copy out of the repo: semgrep skips untracked files and the default .semgrepignore
    # skips tests/, either of which would make this a vacuous (zero-file) scan.
    target = tmp_path / "fixtures"
    shutil.copytree(ROOT / "tests" / "repo_hygiene" / "fixtures", target)
    r = subprocess.run(
        [
            *_semgrep_cmd(),
            "scan",
            "--metrics=off",
            "--error",
            "--json",
            "--config",
            str(ROOT / ".semgrep" / "quartet.yml"),
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert r.returncode == 1, r.stderr
    for rule in ("quartet-no-shell-true", "quartet-no-os-system", "quartet-no-fstring-sql"):
        assert rule in r.stdout
