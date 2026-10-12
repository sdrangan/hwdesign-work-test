# 1-D max pool

Build a Vitis HLS kernel in this folder following the Waveflow stream_inband example
(https://sdrangan.github.io/waveflow/docs/examples/stream_inband/) and the command-response
pattern.

Operation: one int16 signal x of length n.  The window of k samples moves by its own size, so
windows do not overlap: for 0 <= i < floor(n/k), y[i] = max of x[k*i + a] over 0 <= a < k.
Samples past the last whole window are read and ignored.  y is int16.

Requirements: 1 <= n <= 128; 1 <= k <= min(n, 8).  Throughput: one input word (two samples)
per clock while a signal is streaming.

Commands: COMPUTE carries tx_id, n and k, and is followed by the signal; its response carries
tx_id and y.  END makes the kernel return.  A parameter outside the limits above is an error,
checked before the signal is read.

Errors go in AXI-Lite status registers (halted, error code, tx_id): an unknown command, an
invalid parameter, and a header or signal burst whose TLAST is early or missing.  On an error
the kernel closes any output burst it started with TLAST and returns without reading more input.
- The first error in stream order wins.  Within one header the order is: header TLAST, then
  unknown command, then invalid parameter.
- The status tx_id is the failing command's tx_id as received, or 0 if the header ended before
  its tx_id arrived.
- Early TLAST on the signal: send y for every whole window received, close that burst with
  TLAST, then report the error.  If no window is whole, send no y burst.
- Missing TLAST on the signal's last word: send all of y, close it with TLAST, then report the
  error.

Verification: check the Python model against three hand-computed examples, and against this
NumPy reference on random signals:
    y = x[:n // k * k].reshape(n // k, k).max(axis=1)
Then compare the model, C simulation and co-simulation bit for bit.  Scenarios include sizes
at the limits, signal lengths that are not a multiple of k, several COMPUTE commands with
different n and k in one activation, and one activation per error, each followed by a fresh
activation that must pass.  Run co-simulation once: one testbench run that walks through every
scenario in order, calling the kernel once per activation with fresh streams, including each
error activation and the fresh activation after it.

results.md has these sections:
- Results: a table of each target and scenario with the measured value, PASS/FAIL and the file
  it came from (co-simulation for timing, synthesis for resources).
- Assumptions: every place this prompt was silent or ambiguous, and what you chose.
- Open Items: questions you could not settle, and anything that failed or was not done.
- Run History: one row per run, with the prompt version, what was built or changed, and what
  passed, so that a later run can continue from here.

## Revision history
| Version | Change | Why |
| --- | --- | --- |
| 1 | First version | |
| 2 | Throughput is one input word (two samples) per clock | Run 1 read "one sample per clock" as one word every 2 cycles (its results.md, item 2) |
| 2 | Error precedence, the status tx_id, and the output after an early or missing signal TLAST are specified | Run 1 had to choose all four (its items 8, 9, 11 and 12); I kept its choices |
| 2 | results.md has Assumptions, Open Items and Run History sections | So the next run can continue from this one, and so I can see what is still open |
