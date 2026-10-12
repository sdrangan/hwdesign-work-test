# 2-D max pool

Build a Vitis HLS kernel in this folder following the Waveflow stream_inband example
(https://sdrangan.github.io/waveflow/docs/examples/stream_inband/) and the command-response
pattern.

Operation: one int16 image X, n0 rows by n1 columns, sent row-major.  For 0 <= i < floor(n0/s0)
and 0 <= j < floor(n1/s1), Y[i, j] = max of X[s0*i + a, s1*j + b] over 0 <= a < k0 and
0 <= b < k1, where X is 0 outside the image.  Y is int16, returned row-major.

Requirements: 1 <= n0, n1 <= 256; 1 <= k0, k1 <= 8; 1 <= s0, s1 <= 8.  Throughput: one
input pixel per clock while an image is streaming.

Commands: COMPUTE carries tx_id, n0, n1, s0, s1, k0 and k1, and is followed by the image; its
response carries tx_id and Y.  END makes the kernel return.  A parameter outside the limits
above is an error, checked before the image is read.

Errors go in AXI-Lite status registers (halted, error code, tx_id): an unknown command, an
invalid parameter, and a header or image burst whose TLAST is early or missing.  On an error
the kernel closes any output burst it started with TLAST and returns without reading more input.

Verification: check the Python model against three hand-computed examples, and against this
NumPy reference on random images:
    Xp = np.pad(X, ((0, k0), (0, k1)))
    Y = sliding_window_view(Xp, (k0, k1))[::s0, ::s1][:n0 // s0, :n1 // s1].max(axis=(2, 3))
Then compare the model, C simulation and co-simulation bit for bit.  Scenarios include sizes
at the limits, k > s and s > k, several COMPUTE commands with different geometry in one
activation, and one activation per error, each followed by a fresh activation that must pass.
Run co-simulation once: one testbench run that walks through every scenario in order, calling
the kernel once per activation with fresh streams, including each error activation and the
fresh activation after it.

In results.md: a table of each target and scenario with the measured value, PASS/FAIL and the
file it came from (co-simulation for timing, synthesis for resources), and a list of every
place this prompt was silent or ambiguous and what you chose.

## Revision history
| Version | Change | Why |
| --- | --- | --- |
| 1 | First version | |
        
