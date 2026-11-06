"""Quartet command line interface."""

import typer

from quartet import __version__
from quartet.config import InsecureEnvFile, InvalidSettings, load_settings
from quartet.doctor import run_checks

app = typer.Typer(help="Quartet: a multi-agent coding orchestrator.", no_args_is_help=True)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"quartet {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Quartet: a multi-agent coding orchestrator."""


@app.command()
def doctor() -> None:
    """Check that the local environment is ready to run Quartet."""
    try:
        settings = load_settings()
    except (InsecureEnvFile, InvalidSettings) as exc:
        typer.echo(f"✗ settings: {exc}")
        raise typer.Exit(code=1) from None
    checks = run_checks(settings)
    for check in checks:
        typer.echo(f"{'✓' if check.ok else '✗'} {check.name}: {check.detail}")
    if not all(c.ok for c in checks):
        raise typer.Exit(code=1)
