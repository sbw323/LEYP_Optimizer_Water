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


# --- A seconds-long optimizer run (used by the optimizer output tests) ---------


@pytest.fixture
def tiny_config(inventory_csv):
    return {
        "master_input_file": inventory_csv,
        "output_base_dir": "out",
        "skip_segmentation": True,
        "genes": {"budget": {"min": 10_000, "max": 200_000},
                  "rehab_trigger": {"min": 1.0, "max": 3.5}},
        "discount_rate": 0.03,
        "simulation": {"seed": 7, "n_replicates": 1},
        "validation": {"n_points": 3},
        "selection": "knee",
        "plateau_tolerance": 0.05,
        "algorithm": {"pop_size": 6, "n_offsprings": 3, "n_gen": 2, "seed": 1},
    }


@pytest.fixture
def tiny_optimizer(tiny_config, tmp_path, monkeypatch):
    """A seconds-long optimizer run on the three-pipe inventory, in tmp_path."""
    monkeypatch.chdir(tmp_path)
    import leyp_optimizer

    monkeypatch.setattr(leyp_optimizer, "load_config", lambda: tiny_config)
    return tmp_path / "out"
