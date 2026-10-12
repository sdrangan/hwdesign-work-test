# 1-D max pool: results

The design is in `maxpool/`. It was built in Waveflow's `stream_inband` frame: one kernel that a host starts, AXI-Stream in and out, and AXI-Lite carrying only status. It was scaffolded with `waveflow_new_accel_project` from the `stream_inband` reference and follows it file for file.

- **Target:** `xc7z020clg484-1`, 10 ns clock, Vitis HLS 2025.1.
- **Stream words:** 32 bits.
- **Build:** one command, `python maxpool_build.py --through summary --force`. In run 2 it exited with code 0.
- **Prompt version:** 2.

All paths below are relative to `maxpool/`. The numbers are from run 2, which is the current design.

## Results

### Measured performance

| Quantity | Measured | Source |
| --- | --- | --- |
| II of the per-word loop (`WORDS`) | **1**, pipeline depth 5, trip count 1..64 (one iteration per input word) | `w32_proj/solution1/syn/report/csynth.rpt` |
| Estimated clock | **9.066 ns**. Target 10 ns, uncertainty 2.70 ns, effective budget 7.30 ns. The `maxpool_Pipeline_WORDS` module's slack is **−1.77 ns**. Vitis warns `[HLS 200-871] Estimated clock period (9.066 ns) exceeds the target` | `w32_proj/solution1/syn/report/maxpool_csynth.rpt`, `csynth.rpt`, `w32_proj/solution1/solution1.log` |
| Latency of the `timing` activation (one COMPUTE with n = 128, k = 8, then END), from `ap_start` to `ap_done` | **85 cycles**: 64 for the signal words, 21 for everything else | `w32_proj/solution1/sim/report/verilog/result.transaction.rpt`, transaction 3 |
| Latency over all 28 activations of the single cosim run | min 6, avg 76, max 628 cycles. Interval min 20, avg 91, max 642. Total 2510 cycles | `w32_proj/solution1/sim/report/maxpool_cosim.rpt` |
| Synthesis latency of the `WORDS` loop | 5..68 cycles (its pipelined module: 7..71). The top function's latency is "?" because the number of commands is data-dependent | `csynth.rpt` |
| Resources | 1250 FF, 1761 LUT, 0 DSP, 0 BRAM, 0 URAM. The `WORDS` pipeline alone uses 744 FF and 1041 LUT | `csynth.rpt`, `results/resources_w32.json` |

### Targets and scenarios

| # | Target or scenario | Required | Measured | Result | Source |
| --- | --- | --- | --- | --- | --- |
| T1 | Throughput: one input word (two samples) per clock while a signal is streaming | 1 word/clock | `WORDS` loop II = 1, one input word per iteration. In cosim, the 64 signal words of n = 128 took 85 cycles for the whole activation. The other 21 cycles cover the 2-word header, the response header, the END header, pipeline fill and the start/done handshake | **PASS** | II: `csynth.rpt` (synthesis). Cycles: `result.transaction.rpt` (cosim) |
| T2 | Clock: 10 ns target, meeting Vitis's own check | estimated ≤ 7.30 ns effective budget (10 ns − 2.70 ns uncertainty), slack ≥ 0 | 9.066 ns estimated, slack −1.77 ns in `maxpool_Pipeline_WORDS`. Vitis warning HLS 200-871 | **FAIL** | `maxpool_csynth.rpt`, `csynth.rpt`, `solution1.log` (synthesis) |
| T3 | Every pipelined loop at II = 1 (framework check, `require_ii1`) | II = 1 | II = 1 | **PASS** | `inspect_synth_w32` step, `results/loop_df_w32.csv` (synthesis) |
| T4 | Resources | no target given | 1250 FF / 1761 LUT / 0 DSP / 0 BRAM | n/a | `csynth.rpt` (synthesis) |
| V1 | `maxpool_eval` against three hand-computed examples | exact | `[3,-1,4,1,-5,9,2]`, k=2 → `[3,4,9]`; `[-7,-3,-9,-2,-8,-1,0,5]`, k=3 → `[-3,-1]`; `[-32768]*7+[-32767]`, k=8 → `[-32767]` | **PASS** | `results/check_eval.txt` (`tests/test_maxpool_eval.py`) |
| V2 | `maxpool_eval` against the prompt's NumPy reference on random signals | exact | every legal (n, k) with 1 ≤ n ≤ 128 and 1 ≤ k ≤ min(n, 8), 3 random full-range int16 signals each (2,988 cases) | **PASS** | `results/check_eval.txt` |
| V3 | Protocol model against the expected responses (computed from intent) | 28/28 | 28/28 | **PASS** | `results/check_model.json` |
| V4 | pysim against the expected responses | every scenario pysim can express | 24/24. The other 4 need a missing TLAST or a truncated header, which a pysim stream cannot produce | **PASS** | `results/check_pysim.json` |
| V5 | C simulation against the expected responses | 28/28 | 28/28 | **PASS** | `results/check_csim.json` |
| V6 | Co-simulation against the expected responses: one run, every scenario in order, fresh streams for each activation | 28/28 | 28/28 | **PASS** | `results/check_cosim.json` |
| V7 | Model, csim and cosim compared bit for bit with each other (words, burst bounds, TLAST, status) | identical | identical in all 28 scenarios | **PASS** | `tools/compare_stages.py` |

Each scenario below is one activation. Every row is checked word for word, on TLAST and on the final status, in the model, csim and cosim. pysim covers every row not marked "no pysim". The cosim cycles come from `result.transaction.rpt`, in the testbench's order. Every row is **PASS** in each of those stages.

| Scenario | What it covers | Expected status | Cosim cycles |
| --- | --- | --- | --- |
| `worked` | the three hand-computed examples, as 3 COMPUTE commands | clean | 63 |
| `limits` | 11 commands: n/k = 128/8, 128/1, 1/1, 8/8, 127/8, 9/8, 7/7, 128/3, 127/5, ±32767/−32768 extremes, 11/2 (lengths that are not a multiple of k, odd n, odd result counts) | clean | 516 |
| `random_multi` | 12 COMPUTE commands with random n and k | clean | 628 |
| `timing` | n = 128, k = 8 | clean | 85 |
| `err_unknown_cmd` | good command, then `cmd_type = 7` | halted, error 4, tx_id 301 | 26 |
| `err_n_zero` | n = 0 | halted, error 3, tx_id 320 | 6 |
| `err_n_too_big` | good command, then n = 129 with its signal sent (left unread) | halted, error 3, tx_id 341 | 24 |
| `err_k_zero` | k = 0 | halted, error 3 | 6 |
| `err_k_too_big` | k = 9, n = 20 | halted, error 3 | 6 |
| `err_k_gt_n` | k = 6, n = 5 | halted, error 3 | 6 |
| `err_hdr_early_tlast` (no pysim) | good command, then a 1-word header with TLAST | halted, error 5, tx_id 421 | 24 |
| `err_hdr_no_tlast` (no pysim) | header with no TLAST | halted, error 6, tx_id 440 | 6 |
| `err_end_no_tlast` (no pysim) | good command, then an END header with no TLAST | halted, error 6, tx_id 0 | 24 |
| `err_sig_early_tlast` | n = 20, k = 3, burst ends after 10 samples: 3 results, TLAST on the last | halted, error 1, tx_id 481 | 43 |
| `err_sig_early_tlast_no_window` | n = 20, k = 8, burst ends after 6 samples: no whole window, so no y burst | halted, error 1, tx_id 500 | 20 |
| `err_sig_no_tlast` (no pysim) | n = 15, k = 4, last word has no TLAST: all 3 results, closed with TLAST | halted, error 2, tx_id 520 | 25 |
| `fresh_after_*` (11 activations) | a fresh activation after each error: COMPUTE (13, 3), COMPUTE (16, 4), END | clean | 52 each |

### Design summary

- **Command header.** Two words, sent as their own burst with TLAST on the last word:
  - word 0: `cmd_type` (uint8: COMPUTE = 0, END = 1) and `tx_id` (uint16);
  - word 1: `n` (uint16) and `k` (uint8).
- **Response.** A 1-word response header carrying `tx_id`, sent as its own burst, then y. There is no footer.
- **Packing.** Two int16 values per word, element 0 in the low half, for both the signal and y. The layout comes from serialized instances; see `maxpool/layout.md`.
- **Kernel loop (changed in run 2).** `maxpool_body_impl.tpp` reads one signal word per iteration of the `WORDS` loop (II = 1).
  - Both samples go through two chained compare/select steps of a running maximum that restarts at each window. A word completes 0, 1 or (for k = 1) 2 windows.
  - Results are paired into output words, so each iteration writes at most one word.
  - In run 1 the loop handled one sample per iteration and read a word every 2 cycles.
- **Closing the output burst.** The last full output word is held back one step. Whichever word turns out to be last, at the end of the signal or at an early TLAST, therefore goes out with TLAST.
- **Errors.** Error codes are in the `error` register, alongside `halted` and `tx_id`:

  | Code | Meaning |
  | --- | --- |
  | 1 | early TLAST on the signal |
  | 2 | missing TLAST on the signal |
  | 3 | bad parameter |
  | 4 | unknown command |
  | 5 | early TLAST on the header |
  | 6 | missing TLAST on the header |

## Assumptions

These are the places where the prompt was silent or ambiguous, and what I chose. Prompt v2 settled run 1's items 2, 8, 9, 11 and 12; they are kept below, marked as now specified.

1. **Stream width and packing.** The prompt gives neither. I used the frame's 32-bit words, with two int16 per word as the Waveflow Int16 array utilities pack them. An odd n leaves the high half of the last word as zero, which the kernel ignores. y is packed the same way.
2. **Throughput** (now specified, v2). One input word, two samples, per clock. I measured it two ways: the `WORDS` loop's II from synthesis, and the cosim cycle count for n = 128 (85 cycles for 64 words). The prompt gives no latency target, so latency is reported but not graded.
3. **Command encoding.** The prompt doesn't give one. `cmd_type` is an 8-bit integer: COMPUTE = 0, END = 1, and any other value is an unknown command.
   - It is an `IntField`, because a Waveflow `EnumField` cannot carry an out-of-range value. With an enum, no unknown command could be sent without packing words by hand.
   - END carries `tx_id`, `n` and `k` as zeros, which are ignored.
4. **Field widths.** `tx_id` is uint16, the frame's choice. `n` is uint16, so values over 128 can be sent and rejected. `k` is uint8, so k = 9 and higher can be sent and rejected.
5. **Response contents.** "Carries tx_id and y" means a response header holding only `tx_id`, followed by the y burst. There is no footer and no echo of n or k.
6. **Header TLAST.** The prompt makes an early or missing header TLAST an error, so each command header is its own burst with TLAST on its last word. This departs from the reference, which doesn't check header TLAST. An early TLAST means TLAST on word 0 of the 2-word header.
7. **Error codes.** The prompt doesn't number them. I kept the frame's codes 0–3 and added 4 = UNKNOWN_CMD, 5 = TLAST_EARLY_HDR and 6 = NO_TLAST_HDR.
8. **Error precedence** (now specified, v2, and the same as run 1's choice). The first error in stream order wins. Within a header the order is header TLAST, then unknown command, then bad parameter. So an END header without TLAST is an error (6), not a clean end.
9. **`tx_id` in the status** (now specified, v2, and the same as run 1's choice). It is the failing command's `tx_id` as received.
   - For an early header TLAST, word 0, which holds `tx_id`, has already arrived.
   - For an END without TLAST it is 0, because END sends a zero `tx_id`.
   - After a clean run it is 0.
10. **n = 0.** Illegal, since 1 ≤ n is required, so it raises BAD_PARAM. With legal parameters k ≤ n, so y always has at least one result.
11. **Early TLAST on the signal** (now specified, v2). Every whole window received is answered and the y burst is closed with TLAST. If no window is whole, there is no y burst; the response header was already sent and closed. The samples of every word received are read.
12. **Missing TLAST on the signal** (now specified, v2). All floor(n/k) results are sent with TLAST on the last word, then the kernel halts with error 2.
13. **"Samples past the last whole window are read and ignored."** The kernel reads exactly ceil(n/2) words and nothing beyond n.
14. **What the error scenarios send.** After the failing command, the host keeps sending: the queued commands, and the full signal burst for a bad parameter. The kernel leaves all of it unread, and the testbench discards it as the host's reset. The leftover input did not disturb the cosim; all 28 activations passed.
15. **The fresh activation after each error.** The prompt doesn't define it. I used two good COMPUTE commands and END.
16. **"Run co-simulation once."** One `cosim_design` call with no scenario filter: the testbench walks all 28 activations in order. The reference's second, traced cosim and its figure scripts were removed.
17. **"Do not build a timing model in Python."** This conflicts with the frame's pysim timing calibration and its pysim-against-cosim timing check. I followed the instruction:
    - pysim is functional only, with no `proc_latency` or `proc_ii`;
    - the `extract_py_timing` and `validate_timing` steps are not in the build;
    - every cycle count here is from RTL co-simulation.
18. **Only 32-bit words.** The scaffold also generated a 64-bit variant. I removed it because the frame fixes 32-bit words.
19. **The clock criterion (new in run 2).** Two readings are possible: "estimated ≤ 10 ns" and "meets Vitis's effective budget of 10 − 2.7 = 7.3 ns". Run 2 passes the first and fails the second, and Vitis itself issues the warning that the estimate "exceeds the target". So T2 is graded **FAIL**. As you instructed, I did not rework the kernel to close timing. Run 1's 6.824 ns passed both readings.
20. **Loop trip-count annotation (new in run 2).** The `WORDS` loop has `LOOP_TRIPCOUNT min=1 max=64` (n ≤ 128). This only affects the latency numbers in the synthesis report. The hardware still loops ceil(n/2) times.
21. **Stage 1 freeze.** `scenarios.py`, `layout.md` and `tests/test_maxpool_eval.py` are hashed in `results/stage1_freeze.sha256`.
    - Run 2 did not edit them: v2 changes no behaviour they encode.
    - One comment in `scenarios.py` (line 225) still says "128 samples at one per clock". I left it so as not to break the freeze.
    - Run 2 re-checked their hashes, and they match; see Open Items.

## Open Items

1. **Clock target FAIL (T2).** The `WORDS` pipeline's estimated clock is 9.066 ns, against an effective budget of 7.30 ns, a slack of −1.77 ns.
   - The likely cause is the per-iteration chain: two dependent compare/select steps on the running maximum, plus the window-position update that feeds the second compare.
   - Possible fixes, not attempted as you instructed:
     - a deeper pipeline, with the window position precomputed one iteration ahead;
     - a pairwise max of the word's two samples before merging with the accumulator, when both are in the same window;
     - accepting the 10 ns target without the uncertainty margin, which needs your call on which criterion applies.
   - Post-route timing (Vivado implementation) was not run, so it is unknown whether the design meets 10 ns after place and route.
2. **The cosim timing extract is still misleading.** `results/cosim_timing_w32.json` reports `transaction_cycles: 6`, which is the minimum over the 28 transactions, not the timing scenario. All per-activation latencies above come from `result.transaction.rpt`.
3. **Stage 1 hash check: closed.** After the review was approved, I recomputed the hashes with Python's `hashlib`, because `sha256sum` and `Get-FileHash` were blocked. All three files match `results/stage1_freeze.sha256`.
4. **Schema validation.** `waveflow_validate_schema` on `maxpool.py` fails with `'NoneType' object has no attribute '__dict__'`, because it runs the file outside `sys.modules`. The schemas were validated through a verbatim copy, `tools/schemas_shim.py`. The schemas were not changed in run 2.
5. **pysim cannot express 4 scenarios.** These are missing TLAST, truncated header, and END without TLAST. They are covered by the model, csim and cosim only.
6. **`meta.json` formatting differs between stages.** The Python and C++ writers format it differently, though the content is identical. `tools/compare_stages.py` compares it as JSON and every other file byte for byte.

## Run History

| Run | Prompt version | What was built or changed | What passed | Notes for the next run |
| --- | --- | --- | --- | --- |
| 1 | 1 | Full design scaffolded from `stream_inband`: schemas, model, pysim, 28 scenarios, hand-written body (`SAMPLES` loop, one sample per iteration, a word read every 2 cycles), csim, csynth, one cosim | Everything: V1–V7 28/28, II = 1, clock 6.824 ns (slack +0.48 ns), `timing` activation 145 cycles | Read "one sample per clock" as 1 sample/clock (corrected in prompt v2) |
| 2 | 2 | Kernel loop rewritten to one input word (two samples) per iteration (`WORDS` loop, II = 1, `LOOP_TRIPCOUNT` 1..64). Model docstring updated. Scenarios, expected responses, schemas and testbench unchanged. Full forced rebuild: check_eval → model → pysim → csim → csynth → cosim → summary | V1–V7 all PASS (model, csim and cosim 28/28 and bit-identical; pysim 24/24). T1 throughput PASS: II = 1 per word, 85 cycles for the n = 128 activation. **T2 clock FAIL**: 9.066 ns estimated, slack −1.77 ns against the 7.30 ns effective budget. Resources 1250 FF / 1761 LUT | Close timing on the `WORDS` loop (Open Item 1) if the uncertainty-adjusted criterion applies. The run was reviewed and approved with the clock FAIL standing. The Stage 1 hashes were re-verified after approval and match |
