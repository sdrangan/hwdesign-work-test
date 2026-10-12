# 1-D max pool

Build a Vitis HLS kernel in this folder following the Waveflow stream_inband
example (https://sdrangan.github.io/waveflow/docs/examples/stream_inband/) and
the command-response pattern.

## Operation
One int16 signal x of length n.  The window of k samples moves by its own size,
so windows do not overlap: for 0 <= i < floor(n/k), y[i] = max of x[k*i + a]
over 0 <= a < k.  Samples past the last whole window are read and ignored.
y is int16.

## Requirements
1 <= n <= 128; 1 <= k <= min(n, 8).  Throughput: one input word (two samples)
per clock while a signal is streaming.

## Commands and parameters
COMPUTE carries tx_id, n and k, and is followed by the signal; its response
carries tx_id and y.  END makes the kernel return.  A parameter outside the
limits above is an error, checked before the signal is read.

## Errors
Errors go in AXI-Lite status registers (halted, error code, tx_id): an unknown
command, an invalid parameter, and a header or signal burst whose TLAST is
early or missing.  On an error the kernel closes any output burst it started
with TLAST and returns without reading more input.
- The first error in stream order wins.  Within one header the order is:
  header TLAST, then unknown command, then invalid parameter.
- The status tx_id is the failing command's tx_id as received, or 0 if the
  header ended before its tx_id arrived.
- Early TLAST on the signal: send y for every whole window received, close that
  burst with TLAST, then report the error.  If no window is whole, send no y
  burst.
- Missing TLAST on the signal's last word: send all of y, close it with TLAST,
  then report the error.

## Verification
Check the Python model against three hand-computed examples, and against this
NumPy reference on random signals:
    y = x[:n // k * k].reshape(n // k, k).max(axis=1)
Then compare the model, C simulation and co-simulation bit for bit.  Scenarios
include sizes at the limits, signal lengths that are not a multiple of k,
several COMPUTE commands with different n and k in one activation, and one
activation per error, each followed by a fresh activation that must pass.  Run
co-simulation once: one testbench run that walks through every scenario in
order, calling the kernel once per activation with fresh streams, including
each error activation and the fresh activation after it.

## results.md
Write results.md with these sections:
- Interface: the command and response layouts, how values pack into words,
  the status registers and the error codes.
- Design: the loop structure, where state is kept, and how the throughput
  target is reached.
- Targets: each target above with the required value, the measured value,
  PASS/FAIL and the file it came from (co-simulation for timing; synthesis
  for clock, II and resources).
- Verification: the model's check against the hand-computed examples and the
  reference; then one row per scenario saying whether the model, C simulation
  and co-simulation agree, and for each error scenario the status it set and
  whether the fresh activation after it passed. Name the source file of each.
- Assumptions: every place this prompt was silent or ambiguous and what you
  chose, apart from interface details already given under Interface.
- Open Items: questions you could not settle, and anything that failed or was
  not done.
- Run History: one row per run, with the prompt version, what was built or
  changed, and what passed, so that a later run can continue from here.

## Revision history
| Version | Change | Why |
| --- | --- | --- |
| 1 | First version | |
