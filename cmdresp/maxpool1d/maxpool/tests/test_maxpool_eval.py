"""Pins down maxpool_eval: three hand-computed examples, then prompt.md's NumPy reference on
random signals over every legal (n, k).

    python -m pytest tests/test_maxpool_eval.py -q     (or: python tests/test_maxpool_eval.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from maxpool import K_MAX, N_MAX, maxpool_eval  # noqa: E402

# Computed by hand, window by window.
WORKED = [
    # [3,-1] -> 3, [4,1] -> 4, [-5,9] -> 9; the trailing 2 is ignored.
    ([3, -1, 4, 1, -5, 9, 2], 2, [3, 4, 9]),
    # [-7,-3,-9] -> -3, [-2,-8,-1] -> -1; the trailing [0, 5] is ignored (5 is the
    # largest sample, so a kernel that pools the tail would get it wrong).
    ([-7, -3, -9, -2, -8, -1, 0, 5], 3, [-3, -1]),
    # One window of 8 at the bottom of the int16 range: the max is the one -32767.
    ([-32768] * 7 + [-32767], 8, [-32767]),
]


def numpy_ref(x: np.ndarray, k: int) -> np.ndarray:
    n = len(x)
    return x[:n // k * k].reshape(n // k, k).max(axis=1)


def test_worked_examples() -> None:
    for x, k, want in WORKED:
        got = maxpool_eval(np.array(x, np.int16), k)
        assert got.dtype == np.int16
        assert got.tolist() == want, (x, k, got.tolist(), want)


def test_random_vs_numpy() -> None:
    rng = np.random.default_rng(7)
    count = 0
    for n in range(1, N_MAX + 1):
        for k in range(1, min(n, K_MAX) + 1):
            for _ in range(3):
                x = rng.integers(-32768, 32767, n, endpoint=True).astype(np.int16)
                got = maxpool_eval(x, k)
                want = numpy_ref(x, k)
                assert got.dtype == np.int16 and np.array_equal(got, want), (n, k)
                count += 1
    assert count == 3 * sum(min(n, K_MAX) for n in range(1, N_MAX + 1))


if __name__ == "__main__":
    test_worked_examples()
    for x, k, want in WORKED:
        print(f"maxpool_eval({x}, k={k}) = {maxpool_eval(np.array(x, np.int16), k).tolist()}"
              f"   hand: {want}")
    test_random_vs_numpy()
    print("random vs NumPy: every legal (n, k), 3 signals each: PASS")
