import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_relative_markdown_links_resolve():
    files = [*(REPO_ROOT / "docs").rglob("*.md"), REPO_ROOT / "SECURITY.md"]
    assert files, "no markdown files scanned"
    assert all(f.exists() for f in files)
    for md in files:
        for target in re.findall(r"\]\(([^)#\s]+)", md.read_text()):
            if not target.startswith(("http://", "https://", "mailto:")):
                assert (md.parent / target).exists(), f"{md}: broken link {target}"
