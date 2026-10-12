# maxpool: acceptance criteria (frame.md §F8)

Every PASS/FAIL below comes from a build step or a Vitis report, not from judgement.
The full write-up is `../../results.md`.

| Criterion | Required | Measured | Result | Source |
| --- | --- | --- | --- | --- |
| `maxpool_eval` vs hand-computed examples | 3/3 match | 3/3 | PASS | `check_eval` → `results/check_eval.txt` |
| `maxpool_eval` vs NumPy reference | all match | 342/342 random signals | PASS | `check_eval` → `results/check_eval.txt` |
| Checker rejects wrong outputs | ≥ 2 rejected | 5/5 rejected | PASS | `check_eval` → `results/check_eval.txt` |
| Protocol model vs expected | all scenarios | 62/62 | PASS | `check_model` → `results/check_model.json` |
| pysim vs expected | well-formed scenarios | 39/39 | PASS | `check_pysim` → `results/check_pysim.json` |
| csim vs expected | all scenarios | 62/62 | PASS | `check_csim` → `results/check_csim.json` |
| cosim vs expected (one run, all cosim scenarios) | all cosim scenarios | 57/57 (57 transactions) | PASS | `check_cosim` → `results/check_cosim.json` |
| pysim timing vs cosim, `timing` scenario | within 20 cycles | 239 vs 231, delta 8 | PASS | `validate_timing` → `results/timing_verdict.json` |
| Signal-loop II | 1 | 1 (depth 4, trip 1–64) | PASS | `inspect_synth` (`require_ii1`) → `results/loop_df.csv` |
| Throughput while streaming | 1 input word / clock | 1.0 cycles/word (`tput_n128` − `tput_n64` = 32 cycles / 32 words) | PASS | `cosim_timing` → `results/cosim_timing.json` |
| Clock | estimated < 10 ns | 13.660 ns (uncertainty 2.70 ns) | **FAIL** | `w32_proj/solution1/syn/report/maxpool_csynth.rpt` |
| Resources | (no target) | 844 FF, 1594 LUT, 0 DSP, 0 BRAM | — | `inspect_synth` → `results/resources.json` |

**Clock FAIL, cause** (from the "Timing violations" section of
`w32_proj/solution1/.autopilot/db/maxpool_Pipeline_VITIS_LOOP_80_2.verbose.sched.rpt`):
the loop-carried window counter `a` is updated for lane 0 and then lane 1 in the same cycle.
That makes two chains of add → `a == k` → and → reset mux, ending in a store: 13.660 ns. The
running max `acc` chain is 9.308 ns. Not reworked, as instructed.

## Timing model

`proc_latency = 40`, `proc_ii = 1`. These are the reference design's values. The first
comparison against cosim for the `timing` scenario (3 COMPUTEs of 128 samples, then END)
gave pysim 239 against cosim 231 cycles. Both measure the whole kernel call, from start to
return, and the difference of 8 is inside the 20-cycle tolerance, so the values were not
refitted. `proc_ii = 1` agrees with the synthesized loop's II and with the measured
1.0 cycles per input word.

## Ambiguities and how they were resolved

These are listed in full under Assumptions in `../../results.md`. The main ones:
- `tx_id` is placed in header word 1, so "0 if the header ended before its tx_id" can occur.
- The command code is a raw uint8, so unknown codes can be sent through the schema.
- END must also carry TLAST on its last header word.
- `n = 0` is `BAD_PARAM`.
- After an error, the scenarios that cosim runs send nothing the kernel will not read. The
  `_q` variants, which queue input behind the error, run in the model and csim only.

## Where the Waveflow machinery got in the way

- **`ArrayUtilsStep(Int16, ...)` wrote `int16_array_utils.h` to the project root, not
  `include/`.** The cause was that `Int16` had no `include_dir`; the reference's `Float32`
  sets it. Fix: `IntField.specialize(..., include_dir="include")`. There was no error
  message; the body would not have found the header.
- **`ExtractCosimTimingStep` (`CosimReportParser`) takes the `min` column of the cosim
  report.** With one cosim run over 57 activations, that is the shortest activation, not the
  timing scenario. I replaced it with `CosimTimingStep`, which reads
  `sim/report/verilog/result.transaction.rpt` (one latency per kernel call, in testbench
  order). In that file the last transaction's interval is `x`, which a numeric-only pattern
  misses ("56 cosim transactions for 57 scenarios").
- **An `EnumField` cannot hold an unknown value,** so a scenario cannot serialize an unknown
  command through it (as `decisions.md` warns). The header uses an `IntField` for the code,
  and the `EnumField` is generated only for the C++ names.
