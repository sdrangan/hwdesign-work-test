"""test_maxpool_eval.py -- pins maxpool_eval down before anything trusts it.

1. Three hand-computed examples (scenarios.EX1..EX3, worked out in layout.md).
2. The NumPy reference from the spec, on random images over random geometry, including
   every corner of the limits.

    python test_maxpool_eval.py          # or: pytest test_maxpool_eval.py
"""
from __future__ import annotations

import itertools

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from maxpool import Geometry, maxpool_eval
from scenarios import EX1_G, EX1_X, EX1_Y, EX2_G, EX2_X, EX2_Y, EX3_G, EX3_X, EX3_Y


def numpy_reference(x: np.ndarray, g: Geometry) -> np.ndarray:
    """The spec's reference, verbatim."""
    X = np.asarray(x, dtype=np.int16).reshape(g.n0, g.n1)
    n0, n1, s0, s1, k0, k1 = g.n0, g.n1, g.s0, g.s1, g.k0, g.k1
    Xp = np.pad(X, ((0, k0), (0, k1)))
    return sliding_window_view(Xp, (k0, k1))[::s0, ::s1][:n0 // s0, :n1 // s1].max(axis=(2, 3))


def test_worked_examples() -> None:
    for name, x, g, y in (("EX1", EX1_X, EX1_G, EX1_Y), ("EX2", EX2_X, EX2_G, EX2_Y),
                          ("EX3", EX3_X, EX3_G, EX3_Y)):
        got = maxpool_eval(x, g)
        print(f"{name} {g}:\n  X = {x.tolist()}\n  Y = {got.tolist()}  (hand: {y.tolist()})")
        assert got.dtype == np.int16
        assert np.array_equal(got, y), f"{name}: {got.tolist()} != {y.tolist()}"


def test_numpy_reference_random() -> None:
    rng = np.random.default_rng(7)
    geoms = [Geometry(int(rng.integers(1, 40)), int(rng.integers(1, 40)),
                      *(int(v) for v in rng.integers(1, 9, size=4))) for _ in range(300)]
    # Every corner of the limits.
    for n0, n1, s0, s1, k0, k1 in itertools.product((1, 256), (1, 256), (1, 8), (1, 8),
                                                     (1, 8), (1, 8)):
        geoms.append(Geometry(n0, n1, s0, s1, k0, k1))
    for g in geoms:
        x = rng.integers(-32768, 32767, size=(g.n0, g.n1), endpoint=True, dtype=np.int16)
        if rng.random() < 0.3:
            x = -np.abs(x).astype(np.int16) - 1          # all negative: padding visible
        want = numpy_reference(x, g)
        got = maxpool_eval(x, g)
        assert got.shape == want.shape and np.array_equal(got, want), g
    print(f"numpy reference: {len(geoms)} random images, all equal")


if __name__ == "__main__":
    test_worked_examples()
    test_numpy_reference_random()
    print("PASS")
