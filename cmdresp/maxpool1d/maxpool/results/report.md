# maxpool: report (frame F8)

The full report is `../../results.md`. It has every scenario, the cosim cycles of each activation, the assumptions, the open items and the run history. This file is the frame's criteria table, for run 2 (prompt v2).

| Criterion | Required | Measured | Result | Source |
| --- | --- | --- | --- | --- |
| `maxpool_eval` vs hand-computed examples | exact | 3/3 | PASS | `check_eval` step, `results/check_eval.txt` |
| `maxpool_eval` vs NumPy reference, random signals | exact | 2,988/2,988 | PASS | `check_eval` step, `results/check_eval.txt` |
| Protocol model vs expected | 28/28 | 28/28 | PASS | `check_model` step, `results/check_model.json` |
| pysim vs expected | every scenario pysim can express | 24/24 | PASS | `check_pysim` step, `results/check_pysim.json` |
| csim vs expected | 28/28 | 28/28 | PASS | `check_csim` step, `results/check_csim.json` |
| cosim vs expected (one run, every scenario) | 28/28 | 28/28 | PASS | `check_cosim` step, `results/check_cosim.json` |
| Throughput: one input word (two samples) per clock | 1 word/clock | `WORDS` loop II = 1; 85 cycles for an n = 128 activation | PASS | `w32_proj/solution1/syn/report/csynth.rpt`; `w32_proj/solution1/sim/report/verilog/result.transaction.rpt` |
| Clock | 10 ns target, ≤ 7.30 ns effective budget (2.70 ns uncertainty) | 9.066 ns estimated, slack −1.77 ns (HLS 200-871 warning) | FAIL | `w32_proj/solution1/syn/report/maxpool_csynth.rpt`, `csynth.rpt` |
| Every pipelined loop at II = 1 | II = 1 | II = 1 | PASS | `inspect_synth_w32` step, `results/loop_df_w32.csv` |
| Resources | no target | 1250 FF, 1761 LUT, 0 DSP, 0 BRAM | n/a | `csynth.rpt` |
| pysim timing vs cosim (frame F7, 20-cycle tolerance) | -- | not run | not run | Not run, because of the instruction not to build a timing model in Python |

**`proc_latency` and `proc_ii`.** Not used. The module's Python body is functional only, and the `extract_py_timing` and `validate_timing` steps are not in the build.
