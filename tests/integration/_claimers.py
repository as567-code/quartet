"""Multi-process claim helpers; module-level so ``spawn`` children can import them."""

import multiprocessing
import sys
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy.orm import Session

from quartet.db.queue import claim
from quartet.db.session import make_engine

_REPO_ROOT = str(Path(__file__).resolve().parents[2])


def _init_child() -> None:
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)


def claim_all(app_url: str, worker_id: str) -> list[str]:
    """Claim until the queue is empty; return claimed task ids as strings."""
    engine = make_engine(SecretStr(app_url))
    claimed: list[str] = []
    try:
        with Session(engine) as session:
            while (task := claim(session, worker_id, 60)) is not None:
                claimed.append(str(task.task_id))
    finally:
        engine.dispose()
    return claimed


def run_claimers(app_url: str, processes: int) -> list[str]:
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes, initializer=_init_child) as pool:
        results = pool.starmap(claim_all, [(app_url, f"w{i}") for i in range(processes)])
    return [tid for r in results for tid in r]
