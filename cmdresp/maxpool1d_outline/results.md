# 1-D max pool: results

Built with Waveflow in the `stream_inband` frame (command-response pattern), scaffolded
by `waveflow_new_accel_project` into `maxpool/` and adapted from the reference design.
All paths below are relative to `maxpool/`. Everything was run with
`python maxpool_build.py --through summary` (Vitis HLS 2025.1, `xc7z020clg484-1`, 10 ns clock).

**Bottom line:** the function and protocol are correct in every layer: the model, pysim,
csim and cosim all match the expected responses bit for bit on every scenario they ran.
Throughput is one input word per clock and the signal loop reaches II = 1. **The clock target
FAILS:** Vitis estimates 13.660 ns against the 10 ns target. As instructed, the kernel was not
reworked to close timing.

## Interface

**Ports** (generated boundary, `gen/maxpool.cpp`): `s_in` and `m_out` are 32-bit AXI4-Stream.
AXI-Lite bundle `control` carries `ap_start`/`ap_done` and three read-only status registers.

**Command header** `MaxpoolCmdHdr`, 2 words. The layout comes from serializing instances
(`probe_layout.py`, recorded in `layout.md`):

| Word | Bits | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 7:0 | `cmd_type` (uint8) | `COMPUTE = 0`, `END = 1`; any other code is unknown |
| 0 | 23:8 | `n` (uint16) | signal length in samples (0 on END) |
| 0 | 31:24 | -- | zero |
| 1 | 15:0 | `k` (uint16) | window size (0 on END) |
| 1 | 31:16 | `tx_id` (uint16) | command ID (0 on END) |

A header is one burst with TLAST on word 1 only.

**COMPUTE** is followed by the signal: `ceil(n/2)` words with TLAST on the last. **END** is the
header alone.

**Response** (COMPUTE only):
1. `MaxpoolRespHdr`: 1 word with `tx_id` in bits 15:0, as its own burst with TLAST.
2. `y`: `floor(n/k)` int16 values in `ceil(floor(n/k)/2)` words, as one burst with TLAST on
   the last word.

There is no footer.

**Packing:** int16 samples go two to a word, through the generated `int16_array_utils`
(Python: `write_array`/`read_array` with `Int16`). Element `2i` goes in bits 15:0 of word `i`
and element `2i+1` in bits 31:16. With odd `n`, the high half of the last signal word is
padding: the host sends 0 and the kernel ignores it. With an odd count of `y`, the high half
of the last `y` word is 0. Nothing is packed by hand: headers go through their schemas, and
samples through the array utilities.

**Status registers:**

| Register | Width | Meaning |
| --- | --- | --- |
| `halted` | 1 | 1 if the kernel returned on an error |
| `error` | 8 | `MaxpoolError` code |
| `tx_id` | 16 | the failing command's `tx_id` as received; 0 if the header ended before word 1, or after a clean run |

The kernel clears all three at the start of every activation.

**Error codes** (`MaxpoolError`):

| Code | Name | Raised when |
| --- | --- | --- |
| 0 | `NO_ERROR` | |
| 1 | `TLAST_EARLY_SAMP_IN` | TLAST on a signal word before the last |
| 2 | `NO_TLAST_SAMP_IN` | the last signal word has no TLAST |
| 3 | `BAD_PARAM` | COMPUTE with `n` outside 1..128 or `k` outside 1..min(n, 8) |
| 4 | `TLAST_EARLY_HDR` | TLAST on header word 0 |
| 5 | `NO_TLAST_HDR` | header word 1 has no TLAST |
| 6 | `UNKNOWN_CMD` | `cmd_type` is not 0 or 1 |

## Design

- **Loop structure** (`maxpool_body_impl.tpp`):
  - An outer, unpipelined command loop runs once per header. It reads the header with the
    generated `read_axi4_stream(s, tl)`, which stops at an early TLAST.
  - It checks the header's TLAST, then the command code, then `n`/`k`. It returns on END or on
    an error. Otherwise it runs `transaction()`.
  - `transaction()` writes the response header, then runs one pipelined signal loop
    (`#pragma HLS PIPELINE II=1`, trip count 1–64) that reads one word (two samples) per
    iteration.
  - In each iteration, both lanes are unrolled. Each sample updates the running max `acc` and
    the window position `a`. When `a == k`, `acc` goes into the output lane buffer `y_word`.
- **Where state is kept:** in registers local to one command: `acc`, `a`, `y_word`, the
  count `ny`, and a held output word `held`/`held_valid`. Nothing persists between commands
  or activations, and there is no memory.
- **Why there is a held word:** a full `y_word` is not written at once. It replaces `held`,
  and the previous `held` is written without TLAST. So when the signal ends (normally, early,
  or without TLAST), the last `y` word is still in hand and can carry TLAST, even if no window
  completed in the last input word. At most one output word is written per iteration, and
  the flush after the loop writes the last one or two words.
- **Errors:** the kernel sets the status, has already closed any `y` burst, and returns
  without reading more. If an early TLAST leaves no whole window, no `y` burst is sent.
- **How the throughput target is reached:** the signal loop consumes one input word per
  iteration at II = 1. The case with the most output is `k = 1`: each input word produces two
  `y`, which is one output word per input word, so the output keeps up.

## Targets

| Target | Required | Measured | Result | Source |
| --- | --- | --- | --- | --- |
| Throughput while streaming | 1 input word / clock | **1.0** cycles per input word, from (81 − 49) cycles / 32 extra words between `tput_n128` and `tput_n64` | PASS | co-simulation: `w32_proj/solution1/sim/report/verilog/result.transaction.rpt` → `results/cosim_timing.json` |
| Signal-loop II | 1 | **II = 1** achieved (depth 4, trip 1–64) | PASS | synthesis: `w32_proj/solution1/syn/report/maxpool_Pipeline_VITIS_LOOP_80_2_csynth.rpt`, `results/loop_df.csv` (`inspect_synth`, `require_ii1`) |
| Clock | estimated < 10 ns | **13.660 ns** estimated (uncertainty 2.70 ns) | **FAIL** | synthesis: `w32_proj/solution1/syn/report/maxpool_csynth.rpt` |
| pysim vs cosim timing | within 20 cycles | pysim 239 vs cosim 231 cycles, delta 8 | PASS | `results/timing_verdict.json` (`validate_timing`) |
| Bit-exact model / pysim / csim / cosim | all match expected | all match | PASS | `results/check_*.json` |

**Measured latency** (co-simulation, cycles from `ap_start` to `ap_done` per activation, in
`results/cosim_timing.json`):

| Activation | Latency |
| --- | --- |
| `timing`: 3 × COMPUTE with n = 128 (k = 1, 8, 5), then END: 192 signal words + 8 header words | **231** |
| one COMPUTE, n = 128, k = 1 | 81 |
| one COMPUTE, n = 128, k = 8 | 81 |
| one COMPUTE, n = 64, k = 1 | 49 |
| one COMPUTE, n = 1, k = 1 | 18 |
| END alone | 6 |

Synthesis cannot bound the top-level latency (it shows `?`), because the command loop has a
data-dependent trip count.

**Resources** (synthesis): 844 FF, 1594 LUT, 0 DSP, 0 BRAM. That is about 2 % of the LUTs.

## Verification

**Model checks** (`test_eval.py`, run by the build step `check_eval`; output in
`results/check_eval.txt`):
- Three hand-computed examples, all PASS:
  - x = [3,−1,4,1,−5,9,2,6], k = 2 → [3,4,9,6]
  - x = [1,5,2,7,3,0,9], k = 3 → [5,7]
  - x = [−32768,−7,−32768,−32768,100], k = 4 → [−7]
- The NumPy reference `y = x[:n//k*k].reshape(n//k,k).max(axis=1)` on 342 random signals:
  every (n, k) for n ∈ {1,2,7,8,9,127,128}, plus 300 random pairs. All PASS.
- The checker rejects five deliberately wrong outputs:
  - a partial window emitted
  - an early TLAST that does not halt
  - a burst left open at the error
  - the wrong precedence (UNKNOWN_CMD over NO_TLAST_HDR)
  - a `tx_id` reported from a header word that never arrived

**Scenarios.** Expected responses are computed from each scenario's intent (`scenarios.py`).
Each stage is checked against them: words, burst boundaries, TLAST and status, exactly. The
sources are:
- model: `results/check_model.json`
- pysim: `results/check_pysim.json`
- csim: `results/check_csim.json`
- cosim: `results/check_cosim.json`

Co-simulation ran once: one testbench run walked through the 57 scenarios in
`data/w32/scenarios_cosim.txt` in order, one kernel call per activation with fresh streams.
Vitis recorded 57 transactions. In the table, "agree" means all of these match the expected
response bit for bit. "n/r" means not run: pysim runs only error-free activations, and cosim
skips the `_q` scenarios (see Assumptions).

| Scenario | Model | pysim | csim | cosim |
| --- | --- | --- | --- | --- |
| `lim_n1_k1`, `lim_n128_k8`, `lim_n128_k1`, `lim_n8_k8`, `lim_n2_k2`, `lim_txid` (tx_id 0xFFFF and 0) | agree | agree | agree | agree |
| `values_edge`: int16 extremes, ties, all-negative windows | agree | agree | agree | agree |
| `rem_n10_k3`, `rem_n127_k8`, `rem_n7_k6`, `rem_n100_k7`: n not a multiple of k, odd n | agree | agree | agree | agree |
| `multi_cmd`: 6 COMPUTEs with different n and k | agree | agree | agree | agree |
| `end_only` | agree | agree | agree | agree |
| `timing`, `tput_n64`, `tput_n128` | agree | agree | agree | agree |

Error scenarios. Every one is followed by a fresh activation, `<name>__fresh`, which passed in
the model, pysim, csim and, where it ran, cosim. The status column is the status the error set,
`(halted, error, tx_id)`; all stages produced it.

| Scenario | Status set | Model / csim / cosim | Fresh activation after |
| --- | --- | --- | --- |
| `err_unknown_cmd` (cmd 5, after a good command) | (1, 6 UNKNOWN_CMD, 41) | agree / agree / agree | passed |
| `err_bad_n0` | (1, 3 BAD_PARAM, 42) | agree / agree / agree | passed |
| `err_bad_n129` | (1, 3, 43) | agree / agree / agree | passed |
| `err_bad_k0` | (1, 3, 44) | agree / agree / agree | passed |
| `err_bad_k9` (n = 100) | (1, 3, 45) | agree / agree / agree | passed |
| `err_bad_k_gt_n` (n = 5, k = 6, after a good command) | (1, 3, 47) | agree / agree / agree | passed |
| `err_hdr_early` (TLAST on header word 0) | (1, 4 TLAST_EARLY_HDR, 0) | agree / agree / agree | passed |
| `err_hdr_missing` | (1, 5 NO_TLAST_HDR, 51) | agree / agree / agree | passed |
| `err_end_missing` (END without TLAST) | (1, 5, 0) | agree / agree / agree | passed |
| `err_prec_early_cmd` (early TLAST + unknown cmd) | (1, 4, 0) | agree / agree / agree | passed |
| `err_prec_missing_cmd` (no TLAST + unknown cmd) | (1, 5, 54) | agree / agree / agree | passed |
| `err_prec_cmd_param` (unknown cmd + bad n, k) | (1, 6, 55) | agree / agree / agree | passed |
| `err_sig_early` (n = 20, k = 3, 8 samples sent → 2 y) | (1, 1 TLAST_EARLY_SAMP_IN, 60) | agree / agree / agree | passed |
| `err_sig_early_held` (n = 16, k = 4, 10 sent; both y completed before the TLAST word) | (1, 1, 61) | agree / agree / agree | passed |
| `err_sig_early_none` (n = 20, k = 8, 6 sent → no y burst) | (1, 1, 63) | agree / agree / agree | passed |
| `err_sig_early_k1` (TLAST on word 0) | (1, 1, 64) | agree / agree / agree | passed |
| `err_sig_missing` (n = 11, k = 2 → all 5 y, then error) | (1, 2 NO_TLAST_SAMP_IN, 65) | agree / agree / agree | passed |
| `err_sig_missing_one` (n = 1) | (1, 2, 66) | agree / agree / agree | passed |
| `err_unknown_cmd_q` (commands queued behind) | (1, 6, 70), queued words left unread | agree / agree / n/r | passed (also in cosim) |
| `err_bad_param_q` (full signal sent, never read) | (1, 3, 72), signal and queued words left unread | agree / agree / n/r | passed (also in cosim) |
| `err_hdr_early_q` (header word 0 only, then queued commands) | (1, 4, 0), queued words left unread | agree / agree / n/r | passed (also in cosim) |
| `err_sig_early_q` | (1, 1, 76), 7 words left unread | agree / agree / n/r | passed (also in cosim) |
| `err_sig_missing_q` | (1, 2, 78), 7 words left unread | agree / agree / n/r | passed (also in cosim) |

The csim testbench prints the number of input words each activation left unread
(`unread_words`). I saw 7 for `err_sig_early_q` and `err_sig_missing_q`: exactly the queued
COMPUTE and END. I did not capture the count for the other three `_q` scenarios, and the
checker does not compare it.

## Assumptions

- **Process:** the frame's process says to stop after Stage 1 for review. The request was a
  full build in a non-interactive session, so I kept the order instead:
  - Stage 1 (schemas, `maxpool_eval`, scenarios with expected responses, `layout.md`,
    `test_eval.py`) was written and checked before any C++.
  - It was not edited afterwards, apart from adding `include_dir` to `Int16` so its generated
    header lands in `include/`. That changes no values.
- **Width:** 32-bit words only, two samples per word, as the throughput requirement says. The
  scaffold's 64-bit variant was removed.
- **Header field order:** `cmd_type, n, k, tx_id`. This puts `tx_id` in word 1, so the rule
  "tx_id is 0 if the header ended before it arrived" can actually occur and is tested.
- **Header TLAST:** headers are their own bursts and must have TLAST on the last word only.
  This applies to END as well; a missing TLAST on END is NO_TLAST_HDR with `tx_id` 0.
- **Command code:** an 8-bit raw field, so the unknown codes 2–255 can be sent through the
  schema. The C++ compares the code against the generated `MaxpoolCmdType` enum names.
- **END carries** `n = k = tx_id = 0`, and these are ignored. Unknown-command errors report the
  header's `tx_id`.
- **Bad n / k fields** are 16 bits wide, so out-of-range values can be expressed. `n = 0` is
  an invalid parameter, not an empty command.
- **What the host sends after an error:** after a header error, nothing more. After a
  signal-burst error, exactly the sent words. This applies in every scenario that cosim runs,
  because a co-simulated stream cannot leave input unread (cosim replays every word the C
  testbench wrote). The `_q` variants, which queue input behind the failing command
  (including the full signal for BAD_PARAM), run in the model and csim only.
- **Response header** carries only `tx_id`. The number of `y` values is implied by `n`, `k`,
  and TLAST.
- **Early TLAST on the signal** counts every sample in the received words, capped at `n`;
  only whole windows produce `y`. The response header has already been sent, because the
  parameters were valid; only the `y` burst is omitted when no window is whole.
- **No arithmetic:** max is exact on int16, so rounding and saturation do not apply.
- **Timing model:** the pysim `proc_latency = 40` / `proc_ii = 1` left over from the reference
  already matched cosim within tolerance (239 vs 231), so it was not refitted.

## Open Items

- **FAIL: clock.** The estimated clock is 13.660 ns against 10 ns. I did not rework the kernel,
  as instructed. Throughput holds at 1 word per cycle, but at the estimated clock that is
  about 73 M words/s rather than 100 M words/s.
  - **Where the time goes** (from
    `w32_proj/solution1/.autopilot/db/maxpool_Pipeline_VITIS_LOOP_80_2.verbose.sched.rpt`,
    "Timing violations", State 2): the path is the window counter `a`, updated once per lane
    and chained across both lanes of a word in a single cycle:

    ```
    load a → add (1.735) → a == k (1.735) → and (0.978) → reset mux (1.588)
           → add (1.735) → a == k (1.735) → and (0.978) → reset mux (1.588) → store (1.588)
    = 13.660 ns
    ```

  - Because `a` is carried from one iteration to the next and the loop runs at II = 1, Vitis
    cannot split this chain across pipeline stages.
  - The running max `acc` is the next-longest path: compare → select twice, 9.308 ns (State 3).
    It fits 10 ns, but only just.
  - The other states are 6.587 ns and 4.763 ns.
  - To fix it, the per-lane counter recurrence would have to become one update per word.
    That would be a redesign, which was not done here.
- **Synthesis only:** no Vivado implementation was run, so post-route timing is unknown.
- **No VCD:** the reference's error-path waveform (VCD) step was dropped.

## Run History

| Run | Prompt version | Built / changed | Passed |
| --- | --- | --- | --- |
| 1 | 1 | Scaffolded `maxpool/` (stream_inband). Stage 1 written: schemas, `maxpool_eval`, 57 + 5 scenarios, `layout.md`, `test_eval.py` | `check_eval` (hand examples, NumPy reference, 5 rejections), `check_model` 62/62 |
| 1 | 1 | Stage 2: `maxpool_stream_model`, pysim module, C++ body, testbench, `run.tcl`, single-width build DAG | `check_pysim` 39/39, `check_csim` 62/62 on the first try |
| 1 | 1 | csynth plus one cosim over 57 activations | `check_cosim` 57/57; II = 1; clock 13.660 ns **FAIL**; 1.0 cycles/word |
| 1 | 1 | Fixed the parser for the cosim transaction report (plain text; the last row has interval `x`); timing validation | `validate_timing` delta 8 ≤ 20; `summary` written |

| 2 | 1 | After approval: removed the unused scaffold files (`maxpool_figures.py`, `timing_analysis.py`, `dfx_runtime.txt`); wrote the frame's criteria table, `maxpool/results/report.md` | build `--through summary`: all steps up to date, nothing re-run |
| 3 | 1 | Read the synthesis schedule report to find the critical path; no design change | path identified: the window counter `a` chained across both lanes, 13.660 ns (see Open Items) |

To continue from here: `python maxpool/maxpool_build.py --through summary`. Every step is
up to date except those touched by your edits.
