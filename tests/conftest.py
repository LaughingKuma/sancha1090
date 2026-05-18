"""Shared fixtures for DAG and pipeline tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from airflow.models import DagBag


REPO_ROOT = Path(__file__).resolve().parent.parent
DAGS_FOLDER = REPO_ROOT / "dags"


@pytest.fixture(scope="session")
def dagbag() -> DagBag:
    """Parse the project's DAGs once per test session."""
    return DagBag(dag_folder=str(DAGS_FOLDER), include_examples=False)
