"""
checkpoint.py

Atomic file writes and NSGA-II checkpoint/resume utilities.

Replaces the agent-orchestration config.checkpoint module with a
standalone utility suitable for direct use in the LEYP-Water package.
"""

import os
import pickle
import tempfile
from typing import Any, Callable


from pymoo.core.callback import Callback


class _CkptCallback(Callback):
    """pymoo callback that saves checkpoints after each generation.

    Defined at module level (not inside a method) so that pickle can
    serialize it — local classes have un-resolvable __qualname__ and
    cause ``AttributeError: Can't pickle local object``.
    """

    def __init__(self, ckpt):
        super().__init__()
        self.ckpt = ckpt

    def notify(self, algorithm):
        gen = algorithm.n_gen
        if gen % self.ckpt.save_every_n_gen == 0:
            self.ckpt._save(algorithm, gen)


# ============================================================
# Atomic File Write
# ============================================================

def safe_write_file(path: str, content: str) -> None:
    """Write content to a file atomically via temp-file + rename.

    Ensures no half-written files exist if the process is killed
    mid-write (e.g., GCP spot VM preemption).

    Args:
        path: Destination file path.
        content: String content to write.
    """
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)       # atomic on POSIX
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ============================================================
# NSGA-II Optimization Checkpoint
# ============================================================

class OptimizationCheckpoint:
    """Pickle-based checkpoint for pymoo NSGA-II algorithm state.

    Saves the algorithm object after every *save_every_n_gen* generations
    so that optimization can resume from the last completed generation
    after a crash or preemption event.

    Usage::

        ckpt = OptimizationCheckpoint("nsga2_checkpoint.pkl", save_every_n_gen=1)
        algorithm = ckpt.restore_or_create(lambda: NSGA2(...))
        res = minimize(problem, algorithm, callback=ckpt.get_callback(), ...)
        ckpt.cleanup()   # remove checkpoint after successful completion
    """

    def __init__(self, checkpoint_path: str, save_every_n_gen: int = 1):
        self.checkpoint_path = checkpoint_path
        self.save_every_n_gen = save_every_n_gen
        self.resumed_from_gen: int = 0

    # ----------------------------------------------------------
    # Restore / Create
    # ----------------------------------------------------------

    def restore_or_create(self, factory: Callable) -> Any:
        """Load algorithm from checkpoint or create a fresh one.

        Args:
            factory: Zero-argument callable that returns a new pymoo
                     Algorithm instance (e.g., ``lambda: NSGA2(...)``).

        Returns:
            The restored or newly created algorithm object.
        """
        if os.path.exists(self.checkpoint_path):
            try:
                with open(self.checkpoint_path, "rb") as f:
                    data = pickle.load(f)
                algorithm = data["algorithm"]
                self.resumed_from_gen = data.get("n_gen", 0)
                print(f"[Checkpoint] Restored state from {self.checkpoint_path} "
                      f"(gen {self.resumed_from_gen})")
                return algorithm
            except Exception as e:
                print(f"[Checkpoint] Failed to load {self.checkpoint_path}: {e}")
                print("[Checkpoint] Starting fresh.")

        self.resumed_from_gen = 0
        return factory()

    # ----------------------------------------------------------
    # pymoo Callback
    # ----------------------------------------------------------

    def get_callback(self):
        """Return a pymoo-compatible callback that saves checkpoints.

        Returns:
            A callable(algorithm) invoked by pymoo after each generation.
        """
        return _CkptCallback(self)

    # ----------------------------------------------------------
    # Internal save / cleanup
    # ----------------------------------------------------------

    def _save(self, algorithm: Any, n_gen: int) -> None:
        """Persist algorithm state to checkpoint file (atomic)."""
        data = {"algorithm": algorithm, "n_gen": n_gen}
        dir_name = os.path.dirname(self.checkpoint_path) or "."
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                pickle.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.checkpoint_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def cleanup(self) -> None:
        """Remove checkpoint file after successful completion."""
        if os.path.exists(self.checkpoint_path):
            os.unlink(self.checkpoint_path)
            print(f"[Checkpoint] Cleaned up {self.checkpoint_path}")