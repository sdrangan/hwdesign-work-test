# Command-driven FIR filter accelerator

Build a Vitis HLS streaming FIR filter that is controlled entirely by in-band
commands, following the Waveflow `stream_inband` reference design (the streaming
polynomial). Put the final summary in `results.md` (see the last section).

## Function

The kernel filters a stream of signed 16-bit samples with up to 32 taps and
optionally decimates the output:

    acc[n] = sum_{k=0}^{ntaps-1} h[k] * x[n-k]
    y[n]   = sat16( (acc[n] + 2^14) >> 15 )        (arithmetic shift)

- `x`, `h` and `y` are Q1.15: signed 16-bit, 15 fractional bits.
- `acc` is exact: no rounding or overflow before the final step. 37 bits is
  enough; use at least that.
- Rounding is round-half-up (add 2^14, then arithmetic shift right by 15).
  Saturation clips to [-32768, 32767].
- `x[n-k]` reaches back into earlier `PROCESS` commands: the delay line persists
  between commands. It starts at zero and is reset to zero only by a successful
  `LOAD_TAPS`, by a `PROCESS` with `reset = 1`, or by an error (see below).
- With decimation factor `D`, the kernel outputs `y[n]` only for
  `n = 0, D, 2D, ...`, where `n` counts samples within the current command. It
  still updates the delay line with every input sample.

## Interfaces

- `in_stream`, `out_stream`: AXI4-Stream, `WORD_BW` bits wide, with TLAST.
- `WORD_BW` is a build parameter. Build and test both 32 and 64.
- No other ports: the kernel is free-running (`ap_ctrl_none`, as in the reference
  design) and is ready for the next command as soon as it finishes one.
- Samples and taps are packed `WORD_BW/16` per word, lowest index in the lowest
  bits. Unused lanes of a final partial word are zero on output and ignored on
  input.

## Commands and responses

Every command begins with a command header burst. Every `|` below is a TLAST.

| Command | `in_stream` | `out_stream` |
| --- | --- | --- |
| `LOAD_TAPS` (opcode 1) | `CmdHdr` \| `h[0..ntaps-1]` | `RespHdr` \| `RespFtr` |
| `PROCESS` (opcode 2) | `CmdHdr` \| `x[0..nsamp-1]` | `RespHdr` \| `y[...]` \| `RespFtr` |
| `STATUS` (opcode 3) | `CmdHdr` | `RespHdr` \| `StatusMsg` \| `RespFtr` |

Message fields:

- `CmdHdr`: `tx_id` (u16), `opcode` (u8), `count` (u16: `ntaps` for
  `LOAD_TAPS`, `nsamp` for `PROCESS`, ignored for `STATUS`), `decim` (u8: `D`,
  `PROCESS` only), `reset` (u1: `PROCESS` only).
- `RespHdr`: `tx_id` and `opcode`, both copied from the command.
- `RespFtr`: `nin` (u16, input values consumed from the payload burst), `nout`
  (u16, output samples written), `error` (enum below).
- `StatusMsg`: `ntaps` (u8, 0 if no taps are loaded), `nsamp_total` (u32,
  samples filtered since the last successful `LOAD_TAPS`), `nerr` (u16, errors
  since power-up, saturating), `last_error` (the enum).

Define the messages as Waveflow schemas, so the kernel, the testbench and the
Python model share one definition of every bit.

## Errors

| Code | Name | When |
| --- | --- | --- |
| 0 | `NO_ERROR` | |
| 1 | `BAD_OPCODE` | opcode is not 1, 2 or 3 |
| 2 | `BAD_NTAPS` | `LOAD_TAPS` with `ntaps` = 0 or > 32 |
| 3 | `NO_TAPS` | `PROCESS` before any successful `LOAD_TAPS` |
| 4 | `BAD_DECIM` | `decim` not in {1, 2, 4}, or `nsamp` not a multiple of `decim` |
| 5 | `TLAST_EARLY` | TLAST arrives before the burst's declared length |
| 6 | `NO_TLAST` | the burst reaches its declared length without TLAST |

Rules:

- The kernel never hangs and never stops accepting commands. After any error it
  writes `RespHdr` and `RespFtr` and is ready for the next command.
- A header error (codes 1 to 4) is detected before any payload is read. For
  codes 2 to 4 the kernel discards the payload burst through its TLAST and
  writes no `y` burst. For `BAD_OPCODE` it discards nothing beyond the header.
- For `TLAST_EARLY` during `PROCESS`, the `y` burst holds the outputs already
  computed and ends with TLAST. For `NO_TLAST`, the kernel discards input up to
  and including the next TLAST.
- On any error the delay line is reset to zero. Loaded taps change only on a
  successful `LOAD_TAPS`.
- `nerr` and `last_error` are updated on every error.

## Implementation targets

- Part `xc7z020clg400-1`, 10 ns clock, and synthesis must meet timing.
- Throughput: in `PROCESS`, with `ntaps = 32` and `D = 1`, the kernel accepts
  one input word per clock in steady state at both widths (2 samples per cycle
  at 32 bits, 4 at 64). If that does not fit the part, say what it costs, then
  deliver the highest rate that fits, with the numbers.

## Evaluation

Every PASS has to come from a script or build step, not from reading the code.

1. **Python model.** A bit-exact model of the whole protocol, including the
   errors, delay-line state and counters. Pin the arithmetic down with at least
   eight hand-computed worked examples, including rounding at exactly half and
   saturation in both directions.
2. **Scenarios.** Name each scenario. Each one is a sequence of commands, and
   its expected responses come from the model. Include at least these:
   - load taps, then a single `PROCESS`, with `D` = 1, 2 and 4;
   - **block splitting:** one long signal sent as one `PROCESS`, then as several
     `PROCESS` commands of uneven lengths (including odd lengths, which leave a
     partial last word), must produce identical output;
   - `reset = 1` mid-stream, and reloading taps mid-stream with a different
     `ntaps`;
   - `STATUS` before any load, after processing, and after errors;
   - every error code, each followed by a valid command whose result must be
     correct, which shows the kernel recovered;
   - `ntaps` = 1 and 32; `nsamp` = `D` (the shortest valid command) and
     `nsamp` >= 1000.
3. **C simulation and co-simulation** must match the model bit for bit on every
   scenario at both widths.
4. **Timing** from the co-simulation waveforms: for `PROCESS` with `ntaps = 32`,
   `D = 1`, at both widths and two lengths (e.g. 64 and 1024): latency from the
   first input word to the first output word, steady-state input words per
   cycle, and total cycles per command. Include a timing diagram of one
   `PROCESS` transaction showing every burst.
5. **Synthesis:** achieved clock period, LUT, FF, DSP and BRAM, at both widths.

## Results summary: `results.md`

`results.md`, at the top of this folder, is what gets graded. A reader must be
able to follow it without opening the code. It contains:

- a results table: criterion, target, measured value, PASS/FAIL, and the path of
  the file the number came from. Commit the raw logs and reports it cites:
  csim/cosim logs, the synthesis report, the comparison outputs;
- the word layout of every message at both widths;
- the kernel's top function, and how the testbench drives one command through it;
- the timing figure and table, and the synthesis table;
- every place this spec was silent or ambiguous, and what you chose;
- anything that does not meet the spec, stated plainly as a FAIL, not hidden.
