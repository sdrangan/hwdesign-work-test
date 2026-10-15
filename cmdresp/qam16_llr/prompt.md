# 16-QAM LLR demapper

Build a Vitis HLS kernel in this folder following the Waveflow stream_inband
example (https://sdrangan.github.io/waveflow/docs/examples/stream_inband/) and
the command-response pattern.

## Operation
Max-log soft demapping of Gray-coded 16-QAM.  Each received complex sample
y = yI + j*yQ gives four LLRs, positive meaning the bit is more likely 0.  The
constellation levels are -3, -1, +1, +3 on each axis: bit b0 is 0 when I > 0,
bit b1 is 0 when |I| = 1, and b2, b3 are the same for Q.  With inv_var the
inverse noise variance from the command:

    LLR(b0) = inv_var * yI          LLR(b1) = inv_var * (2 - |yI|)
    LLR(b2) = inv_var * yQ          LLR(b3) = inv_var * (2 - |yQ|)

In integers, with yI, yQ and inv_var as their raw 16-bit values (8 fractional
bits each): p = inv_var * yI (exact, 16 fractional bits), then
LLR(b0) = sat8((p + 2^13) >> 14), an arithmetic shift, so round half up and
saturate to [-128, 127].  For b1, 2 - |yI| is 512 - |yI| (exact, 17 bits
signed; |-32768| is 32768), then the same multiply, round and saturate.  Q is
the same.  Each sample is independent: nothing carries from one sample or one
command to the next.

## Requirements
- yI, yQ: signed 16-bit, 8 fractional bits.
- inv_var: unsigned 16-bit, 8 fractional bits.  0 is valid (every LLR is 0).
- LLRs: signed 8-bit, 2 fractional bits, four per sample, in the order b0,
  b1, b2, b3.
- 1 <= nsamp <= 1024.
- Throughput: one complex sample per clock while samples are streaming.

## Commands and parameters
DEMAP carries tx_id, nsamp and inv_var and is followed by a burst of nsamp
samples; its response carries tx_id and the 4*nsamp LLRs.  END makes the
kernel return.  nsamp outside 1..1024 is an error, checked before any sample
is read.  Each DEMAP has its own inv_var.

## Errors
Errors go in AXI-Lite status registers (halted, error code, tx_id).  On an
error the kernel closes any output burst it started with TLAST and returns
without reading more input.  Nothing persists between activations: the status
registers are cleared at the start of each one.

Errors: an unknown command; nsamp out of range (nothing is sent for that
command); a sample burst whose TLAST comes early (the LLRs of every sample
received are sent, then the burst is closed); and a sample burst with no TLAST
on its last sample (all nsamp samples' LLRs are sent, then the error).  The
first error in stream order wins.

## Verification
Check the Python model against three hand-computed examples, including one
that saturates, and against a NumPy reference that applies the integer
formulas above to random samples, bit for bit.  Keep the scenarios few so the
run is quick: nsamp = 1; nsamp = 1024 with random samples; full-scale samples
with a large inv_var, so every LLR saturates; two DEMAP commands with
different nsamp and inv_var in one activation; and one activation per error,
each followed by a fresh activation that must pass.

Compare the model, C simulation and co-simulation bit for bit.  Run
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
  for clock, II and resources).  Always report the latency and throughput
  from co-simulation and the estimated clock and resources from synthesis,
  writing "no target" where this prompt sets none.
- Verification: the model's check against the hand-computed examples and the
  reference; then one row per scenario saying whether the model, C simulation
  and co-simulation agree, and for each error scenario the status it set and
  whether the fresh activation after it passed. Name the source file of each.
- Assumptions: every place this prompt was silent or ambiguous and what you
  chose, apart from interface details already given under Interface.
- Open Items: questions you could not settle, and anything that failed or was
  not done, or "None".
- Run History: one row per run, with the prompt version, what was built or
  changed, and what passed, so that a later run can continue from here.

Every number and PASS/FAIL in Targets and Verification names the file it came
from (a log, a comparison output or a synthesis report), and that file stays in
this folder to be committed: a reviewer opens them to check results.md.

## Revision history
| Version | Change | Why |
| --- | --- | --- |
| 1 | First version | |
