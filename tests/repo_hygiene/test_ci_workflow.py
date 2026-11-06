import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WF = [yaml.safe_load(p.read_text()) for p in (ROOT / ".github" / "workflows").glob("*.yml")]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_workflows_exist():
    assert WF, "no workflows found"


def test_top_level_permissions_empty():
    for wf in WF:
        assert wf.get("permissions") == {}


def test_every_job_declares_permissions():
    for wf in WF:
        for name, job in wf["jobs"].items():
            assert "permissions" in job, f"job {name} must declare least-privilege permissions"
            assert job["permissions"] == {"contents": "read"}, name


def test_actions_pinned_to_full_sha():
    for wf in WF:
        for job in wf["jobs"].values():
            for step in job.get("steps", []):
                if "uses" in step:
                    uses = step["uses"]
                    assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", uses), uses


def test_no_pull_request_target():
    for wf in WF:
        triggers = wf.get(True, wf.get("on"))  # PyYAML parses `on:` as True
        assert "pull_request_target" not in (triggers or {})


def test_checkout_does_not_persist_credentials():
    for wf in WF:
        for job in wf["jobs"].values():
            for step in job.get("steps", []):
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] is False


def test_expected_jobs_present():
    for wf in WF:
        assert {"lint", "types", "test", "secrets", "sast", "deps", "actions"} <= set(wf["jobs"])


def test_test_job_uses_compose_postgres_image():
    for wf in WF:
        service = wf["jobs"]["test"]["services"]["postgres"]
        assert service["image"] == COMPOSE["services"]["postgres"]["image"]
