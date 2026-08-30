"""Shared fixtures for the LEYP-Water test suite."""

import os
import sys

import numpy as np
import pandas as pd
import pytest

# Modules live at the repository root, not in a package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from leyp_core import Pipe  # noqa: E402


@pytest.fixture(autouse=True)
def _fixed_seed():
    """Seed the global stream so every test starts from the same draw."""
    np.random.seed(4242)


@pytest.fixture
def pipe_attrs():
    """Attributes for a mid-life 6-inch ductile iron main."""
    return {
        "PipeID": "T1",
        "Material": "DI",
        "Diameter": 6.0,
        "Length": 300.0,
        "CoF_Value": 2.0,
        "Age": 50,
        "Condition": 1,
    }


@pytest.fixture
def pipe(pipe_attrs):
    return Pipe(dict(pipe_attrs))


@pytest.fixture
def inventory_csv(tmp_path):
    """A small three-pipe inventory written to a temporary CSV."""
    df = pd.DataFrame(
        [
            {"PipeID": "A", "Material": "DI", "Age": 90, "Length": 200.0,
             "Diameter": 6, "Condition": 1, "CoF_Value": 2},
            {"PipeID": "B", "Material": "AC", "Age": 70, "Length": 150.0,
             "Diameter": 6, "Condition": 1, "CoF_Value": 1},
            {"PipeID": "C", "Material": "PVC", "Age": 30, "Length": 400.0,
             "Diameter": 8, "Condition": 2, "CoF_Value": 3},
        ]
    )
    path = tmp_path / "inventory.csv"
    df.to_csv(path, index=False)
    return str(path)
