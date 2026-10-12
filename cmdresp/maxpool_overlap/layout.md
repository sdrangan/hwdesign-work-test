# Wire layout

Every row below was obtained by serializing an instance with Waveflow
(`scratch/layout_dump.py`: `MaxpoolCmdHdr().serialize(word_bw=32)`,
`MaxpoolRespHdr().serialize(word_bw=32)`, `write_array(..., elem_type=Int16, word_bw=32)`)
and writing down what came out. Stream words are 32 bits.

## Command header (3 words; TLAST on word 2)

`cmd_type=0x5A tx_id=0x1234 n0=0x0100 n1=0x00FF s0=3 s1=4 k0=5 k1=6` serializes to
`0x0012345a 0x00ff0100 0x06050403`:

| Word | Bits | Field | Type |
| --- | --- | --- | --- |
| 0 | 7:0 | `cmd_type` | uint8 (COMPUTE = 0, END = 1, other values: unknown command) |
| 0 | 23:8 | `tx_id` | uint16 |
| 0 | 31:24 | -- | zero |
| 1 | 15:0 | `n0` | uint16 (rows) |
| 1 | 31:16 | `n1` | uint16 (columns) |
| 2 | 7:0 | `s0` | uint8 |
| 2 | 15:8 | `s1` | uint8 |
| 2 | 23:16 | `k0` | uint8 |
| 2 | 31:24 | `k1` | uint8 |

## Response header (1 word; TLAST on it)

`tx_id=0xBEEF` serializes to `0x0000beef`:

| Word | Bits | Field | Type |
| --- | --- | --- | --- |
| 0 | 15:0 | `tx_id` | uint16 |
| 0 | 31:16 | -- | zero |

## Response footer

None: the response carries only `tx_id` and `Y` (prompt), and nothing is known only after
the data.

## Image burst and output burst

Both are `Int16` arrays through the Waveflow array utilities: **two pixels per word,
element 0 in bits 15:0, element 1 in bits 31:16**, two's complement. `[1, -2, 3]` serializes
to `0xfffe0001 0x00000003`: an odd count leaves the high half of the last word zero (the
kernel ignores it on input, and writes zero there on output).

- Image: `n0 * n1` pixels, row-major, `ceil(n0*n1 / 2)` words, TLAST on the last.
- Output: `(n0 // s0) * (n1 // s1)` values of `Y`, row-major, `ceil(m0*m1 / 2)` words, TLAST
  on the last. When `Y` is empty the burst is omitted (the response is the header alone).

## Decisions (`docs/examples/stream_inband/decisions.md`)

1. **Configuration**: in every COMPUTE header (`n0, n1, s0, s1, k0, k1`); nothing over AXI-Lite, nothing kept between commands.
2. **Responses**: COMPUTE gets `RespHdr | Y`; END gets nothing; a failed command gets nothing beyond what it had written when it failed.
3. **Zero-length data**: an image is never empty (n0, n1 >= 1), but `Y` is empty when `n0 < s0` or `n1 < s1`: then the response is the header alone, no data burst -- not an error.
4. **Error precedence**: first detected in stream order. Per header: early header TLAST (stop reading at once), then missing header TLAST, then unknown `cmd_type`, then parameters; all before anything is written or the image is read. Then the image's early or missing TLAST.
5. **Packing**: by the schemas and `Int16` array utilities, as above; unused bits are zero and ignored by the kernel.
6. **Rounding / saturation**: none -- max over int16 is exact; the zero padding is the only arithmetic choice, and an all-negative window that reaches past the image gives 0.
7. **`tx_id` in the status**: the failing command's `tx_id`, read from header word 0 (which even a truncated header delivers); 0 after a clean run.
8. **TLAST on fixed-length bursts**: checked on the command header (the prompt requires it): TLAST before word 2 is `TLAST_EARLY_HDR_IN` (5), none on word 2 is `NO_TLAST_HDR_IN` (6). The image TLAST is checked as in the frame (codes 1, 2).
9. **Unused opcodes**: `cmd_type` is a plain 8-bit field so a host can send one; any value other than 0 and 1 is `BAD_CMD` (4).
10. **Footer**: none.
11. **Early image TLAST (output content)**: the kernel sends exactly the outputs `Y[i,j]` whose last needed pixel `(s0*i+k0-1, min(s1*j+k1-1, n1-1))` arrived (a row-major prefix of `Y`; rows padded below the image are never complete), closes that burst with TLAST if it sent any, and halts. Missing image TLAST: all of `Y`, then halt.
12. **Stream width**: 32 bits only (frame F2); the reference's 64-bit variant is not built.
