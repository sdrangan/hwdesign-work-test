# qam16_llr -- acceptance criteria (frame F8)

Build: `python qam16_llr_build.py --through summary`, run 2, 2026-10-15.  Every PASS below is
the verdict of the named build step or report; none is written by hand.  The full report is
`../results.md`.

| Criterion | Required | Measured | Result | Source |
| --- | --- | --- | --- | --- |
| Function vs hand-computed examples (3, one saturating) | exact | 3 / 3 exact | PASS | `check_eval` step, `results/eval_check.txt` |
| Function vs independent integer reference | bit exact | 12 inv_var x 4144 samples, 0 mismatches | PASS | `check_eval` step, `results/eval_check.txt` |
| Checker rejects wrong outputs | at least 2 | 3 / 3 rejected | PASS | `checker_demo` step, `results/checker_demo.txt` |
| Protocol model vs expected | all scenarios | 12 / 12 | PASS | `check_model` step, `results/check_model.json` |
| pysim vs expected | scenarios pysim can express | 11 / 11 (`no_tlast` cannot be expressed) | PASS | `check_pysim` step, `results/check_pysim.json` |
| csim vs expected | all scenarios | 12 / 12 | PASS | `check_csim` step, `results/check_csim.json` |
| cosim vs expected (one run, all scenarios) | all scenarios | 12 / 12 | PASS | `check_cosim` step, `results/check_cosim.json` |
| pysim timing vs cosim, `rand_1024` | within 20 cycles | 1039 vs 1039, delta 0 | PASS | `cosim_timing` step, `results/cosim_timing.json` |
| Sample loop II | 1 | 1 (depth 5) | PASS | `inspect_synth` step (`require_ii1`), `results/loop_df.csv`, `w32_proj/solution1/syn/report/qam16_llr_Pipeline_samples_csynth.rpt` |
| Estimated clock | < 10 ns | 6.774 ns | PASS | `w32_proj/solution1/syn/report/qam16_llr_csynth.rpt` |
| Throughput while streaming | 1 sample / clock | II = 1; 1024 samples in 1039 cycles | PASS | as above, and `w32_proj/solution1/sim/report/verilog/result.transaction.rpt` |
| Latency, nsamp = 1024 / nsamp = 1 | no target | 1039 / 16 cycles | -- | `results/cosim_timing.json` |
| Resources | no target | 4 DSP, 653 FF, 1029 LUT, 0 BRAM | -- | `results/resources.json` |

## Timing model

`proc_ii = 1` comes from the synthesized sample loop.  `proc_latency` was set to 3 to start
with; pysim then predicted 1031 cycles for `rand_1024` against cosim's 1039 (run 1).  It was
set to 3 + 8 = 11, and run 2 matches exactly.  It is calibrated on that one command length
only.

## Ambiguities

See `../results.md`, Assumptions.  In short:

- 32-bit words only.
- The command field is an 8-bit integer, so an unknown code is `UNKNOWN_CMD` (error 4).
- nsamp = 0 is `BAD_PARAM`.
- An unknown command is checked before the nsamp range.
- No footer.

## Where the Waveflow machinery got in the way

- **Unknown commands.** An `EnumField` cannot serialize a value outside its enum, so an
  unknown-command scenario could not be built through the schema.  `cmd_type` is therefore an
  `IntField`, and the generated `Qam16LlrCmdType` enum is used only for the constants.
- **Missing header.** `include/complex__int16_array_utils.h` includes `wf_cint.h`, which
  `ArrayUtilsStep` does not write.  `gen_include` copies Waveflow's shipped
  `waveflow/build/wf_cint.h` into `include/`, as `examples/schemas/complex` does.  There was no
  error message: the header was simply absent from the generated files.
- **Cosim latency over several calls.** `ExtractCosimTimingStep` reads the minimum latency from
  `<top>_cosim.rpt`, which is wrong for a cosim run with several calls.  The build reads the
  per-call rows of `sim/report/verilog/result.transaction.rpt` instead (`cosim_timing` step).
