# Initial Plan: Conjugate Gradient Matrix Inverse

## Project Team

- Jane Doe
- John Doe

## IP Definition

### What the IP does

The IP computes an approximate inverse `X ≈ Q⁻¹` of an `n × n` symmetric
positive-definite matrix `Q` with the conjugate gradient (CG) method. Matrix
inversion appears throughout scientific computing, estimation and control (for
example, the covariance updates in a Kalman filter), and for moderate `n` it
dominates the running time of those applications on an embedded processor.

CG replaces the data-dependent pivoting of Gaussian elimination with a fixed
sequence of matrix multiplications and column-wise vector updates. Every step
has the same shape regardless of the data, which is what makes it a good fit
for hardware.

### Mathematical operations

CG solves `Q x = b` iteratively. Running it on all `n` columns of the identity
at once gives the inverse. The Python reference model, which will also be our
golden model, is:

```python
import numpy as np

def cginv(Q, nit):
    """Approximate inverse of a symmetric positive-definite n x n matrix Q."""
    n = Q.shape[0]
    X = np.zeros((n, n))
    R = np.eye(n)                 # residual I - Q X, with X = 0
    P = R.copy()                  # search directions, one per column
    rnorm = np.ones(n)            # squared column norms of R
    for _ in range(nit):
        S = Q @ P                                   # matrix multiply unit
        alpha = rnorm / np.sum(P * S, axis=0)       # column-wise dot products
        X = X + P * alpha[None, :]
        R = R - S * alpha[None, :]
        rnorm_new = np.sum(R**2, axis=0)
        beta = rnorm_new / rnorm
        P = R + P * beta[None, :]
        rnorm = rnorm_new
    return X
```

In exact arithmetic `nit = n` iterations give the exact inverse; in practice
fewer suffice when `Q` is well conditioned. We will target `n ≤ 64` in float32.

### Why it suits hardware acceleration

- **Compute bound.** Each iteration is dominated by `S = Q @ P`, which is
  `n³` multiply-adds. Everything else is `O(n²)`.
- **Highly parallel.** The matrix product maps directly onto a systolic array,
  and the `n` columns are independent in every vector update, so they can be
  processed in parallel lanes.
- **Regular.** There are no data-dependent branches or pivoting, so the loops
  can be pipelined with a fixed schedule.

### How it interacts with the PS

The PS writes `Q` into DDR, then sends a single command giving the address of
`Q`, the address where `X` should be written, `n` and `nit`. The IP runs all
iterations without further PS involvement and returns a response with a status
code when `X` is in memory.

## IP Architecture

### Sub-modules

1. **Controller.** Receives the PS command, sequences the iterations and sends
   the response. It holds no matrix data.
2. **Matrix multiply unit.** Computes `S = Q @ P` with an 8 × 8 systolic array,
   tiling larger matrices, following the architecture from class. `Q` is loaded
   into on-chip BRAM once, at the start, and reused every iteration.
3. **Vector update unit.** Given `S` and `P`, computes the column dot products,
   `alpha`, `beta`, and updates `X`, `R` and `P`. It processes several columns
   in parallel, one lane per column.

### Interfaces

| From → To | Interface | What it carries |
|---|---|---|
| PS → Controller | AXI4-Stream command FIFO | `{addr_Q, addr_X, n, nit}` |
| Controller → PS | AXI4-Stream response FIFO | `{status, iterations_run}` |
| Controller → MM unit | `hls::stream` command | `{iteration}`; `iteration = 0` also loads `Q` |
| MM unit ↔ DDR | AXI4 master (`m_axi`) | reads `Q` once at the start |
| MM unit → Vector unit | `hls::stream`, one column per burst | the columns of `S` |
| Vector unit → MM unit | `hls::stream`, one column per burst | the new columns of `P` |
| Vector unit → Controller | `hls::stream` token | `{iteration, rnorm_max}`, for an early stop |
| Vector unit → DDR | AXI4 master (`m_axi`) | writes `X` at the end |

`X`, `R` and `P` live in BRAM inside the vector unit (`3 × 64 × 64 × 4 B =
48 KB`), so DDR traffic is one read of `Q` and one write of `X`. The two
streams between the matrix and vector units form a loop: the vector unit can
start updating column `j` as soon as column `j` of `S` arrives, which overlaps
the two halves of each iteration.

### Why this decomposition

The split follows the two kinds of work in the algorithm: an `O(n³)` matrix
product that wants a 2-D array, and `O(n²)` column-wise updates that want
parallel lanes. Each unit can be tested on its own against the corresponding
lines of `cginv` before they are connected. The matrix multiply unit is the
same block we built in class, so it can be brought up first.

### Existing IP

We will check whether the Vitis BLAS library's GEMM kernel can replace our
matrix multiply unit, and use it if it fits the streaming interface above.
