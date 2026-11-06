import pathlib
import re


def test_relative_markdown_links_resolve():
    for md in [*pathlib.Path("docs").rglob("*.md"), pathlib.Path("SECURITY.md")]:
        for target in re.findall(r"\]\(([^)#\s]+)", md.read_text()):
            if not target.startswith(("http://", "https://", "mailto:")):
                assert (md.parent / target).exists(), f"{md}: broken link {target}"
