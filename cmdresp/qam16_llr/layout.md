# Wire layout

Every row below comes from serializing an instance with Waveflow at `word_bw = 32`
(`Qam16LlrCmdHdr().serialize`, `Qam16LlrRespHdr().serialize`, `arrayutils.write_array`) and
reading the words back:

```
CmdHdr  cmd_type=1, tx_id=0x1234, nsamp=0x0400, inv_var=0xABCD -> [0x00123401, 0xABCD0400]
CmdHdr  cmd_type=7 (same fields)                                -> [0x00123407, 0xABCD0400]
RespHdr tx_id=0x1234                                            -> [0x00001234]
CInt16  [(0x1234, -2), (-1, 0x7FFF)]                            -> [0xFFFE1234, 0x7FFFFFFF]
Int8    [1, -1, 127, -128]                                      -> [0x807FFF01]
```

## Command header (2 words)

| Word | Bits | Field | Type |
| --- | --- | --- | --- |
| 0 | 7:0 | `cmd_type` | uint8: DEMAP = 0, END = 1, anything else unknown |
| 0 | 23:8 | `tx_id` | uint16 |
| 0 | 31:24 | unused, 0 | |
| 1 | 15:0 | `nsamp` | uint16 (1..1024 for DEMAP, 0 for END) |
| 1 | 31:16 | `inv_var` | uint16, raw u8.8 |

## Response header (1 word)

| Word | Bits | Field | Type |
| --- | --- | --- | --- |
| 0 | 15:0 | `tx_id` | uint16 |
| 0 | 31:16 | unused, 0 | |

## Response footer

None (decision 10).

## Sample burst

One complex sample per word (`ComplexField` over int16, `pf = 1`): `yI` (re) in bits 15:0,
`yQ` (im) in bits 31:16, each raw s8.8.  No half-filled words, so an odd `nsamp` needs no rule.

## LLR burst

One word per sample (`Int8` array, `pf = 4`): b0 in bits 7:0, b1 in 15:8, b2 in 23:16, b3 in
31:24, each raw s6.2.  The burst is `nsamp` words, TLAST on the last.

## Decisions (docs/examples/stream_inband/decisions.md)

1. Configuration: `nsamp` and `inv_var` in every DEMAP header; nothing over AXI-Lite.
2. Responses: DEMAP gets `RespHdr | LLR burst`; END gets nothing.
3. Zero-length data: `nsamp = 0` is illegal (prompt: 1..1024): `BAD_PARAM`, nothing written.
4. Error precedence: first in stream order.  Within one header, an unknown `cmd_type` is
   checked before `nsamp`; both before anything is written or any sample read.
5. Packing: as above, by the schemas; unused header bits are sent as 0 and ignored.
6. Rounding/saturation: `sat8((p + 2^13) >> 14)`, arithmetic shift (round half up), products
   exact (33/35 bits), saturation to [-128, 127]; `512 - |y|` on 18 bits signed.
7. `tx_id` in the status: the `tx_id` of the command that failed (the unknown command's own
   `tx_id` field for `UNKNOWN_CMD`); 0 after a successful run.
8. TLAST on fixed-length bursts: not checked on headers; only the sample burst is checked.
9. Unused opcodes: any `cmd_type` other than 0 or 1 is `UNKNOWN_CMD` (error 4).
10. Footer: none.
