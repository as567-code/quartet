import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOC = yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_images_pinned_by_digest():
    for name, svc in DOC["services"].items():
        assert re.search(r"@sha256:[0-9a-f]{64}$", svc["image"]), f"{name} image not digest-pinned"


def test_jaeger_is_v2():
    image = DOC["services"]["jaeger"]["image"]
    assert image.startswith("cr.jaegertracing.io/jaegertracing/jaeger:2.")


def test_ports_bind_loopback_only():
    for name, svc in DOC["services"].items():
        for p in svc.get("ports", []):
            assert str(p).startswith("127.0.0.1:"), f"{name} exposes {p} beyond loopback"


def test_postgres_has_healthcheck_and_no_inline_passwords():
    pg = DOC["services"]["postgres"]
    assert "healthcheck" in pg
    for key in ("POSTGRES_PASSWORD", "QUARTET_MIGRATOR_PASSWORD", "QUARTET_APP_PASSWORD"):
        assert pg["environment"][key].startswith("${"), key


def test_roles_script_is_executable_in_tree():
    script = ROOT / "docker" / "postgres" / "init" / "01-roles.sh"
    assert script.stat().st_mode & 0o111
