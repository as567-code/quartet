import tomllib
from pathlib import Path
from typing import Any

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _cfg() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT.read_text())


def test_provider_sdks_pinned_to_current_majors():
    deps = _cfg()["project"]["dependencies"]
    for spec in (
        "anthropic>=2.6,<3",
        "openai>=3.46,<4",
        "httpx2>=2.16,<3",
        "jsonschema>=4.26,<5",
        "opentelemetry-api>=1.45,<2",
    ):
        assert spec in deps


def test_live_tests_deselected_by_default():
    assert '-m "not live"' in _cfg()["tool"]["pytest"]["ini_options"]["addopts"]
