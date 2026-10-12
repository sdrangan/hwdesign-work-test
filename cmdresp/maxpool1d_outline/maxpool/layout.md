# Wire layout

Every row below was obtained by serializing an instance with Waveflow
(`probe_layout.py`, 32-bit words), not by reading the schema:

```
CmdHdr only cmd_type=0xAA: ['0x000000aa', '0x00000000']
CmdHdr only n=0x1234:      ['0x00123400', '0x00000000']
CmdHdr only k=0x5678:      ['0x00000000', '0x00005678']
CmdHdr only tx_id=0xBEEF:  ['0x00000000', '0xbeef0000']
CmdHdr COMPUTE n=10 k=3 tx_id=7: ['0x00000a00', '0x00070003']
RespHdr tx_id=0xBEEF:      ['0x0000beef']
int16 [1, -2, 3]:          ['0xfffe0001', '0x00000003']
```

## Command header (`MaxpoolCmdHdr`, 2 words)

| Word | Bits | Field | Type |
| --- | --- | --- | --- |
| 0 | 7:0 | `cmd_type` | uint8 code: `COMPUTE = 0`, `END = 1`, anything else unknown |
| 0 | 23:8 | `n` | uint16, signal length in samples (0 on END) |
| 0 | 31:24 | -- | zero |
| 1 | 15:0 | `k` | uint16, window size (0 on END) |
| 1 | 31:16 | `tx_id` | uint16 |

TLAST must be on word 1 and only on word 1. TLAST on word 0 is
`TLAST_EARLY_HDR`, and then `tx_id` has not arrived: status `tx_id = 0`.

## Response header (`MaxpoolRespHdr`, 1 word)

| Word | Bits | Field | Type |
| --- | --- | --- | --- |
| 0 | 15:0 | `tx_id` | uint16, echo of the COMPUTE command's `tx_id` |
| 0 | 31:16 | -- | zero |

Sent as its own burst, TLAST on its only word.

## Response footer

None: the response carries only `tx_id` and `y`.

## Sample burst (signal `x`) and result burst (`y`)

int16, two per 32-bit word, packed by the Waveflow array utilities
(`int16_array_utils`): element `2i` in bits 15:0 of word `i`, element `2i+1` in
bits 31:16. The signal is `ceil(n/2)` words, TLAST on the last. With odd `n`
the high half of the last word is padding: the host sends zero, the kernel
ignores it. `y` is `floor(n/k)` values in `ceil(floor(n/k)/2)` words, TLAST on
the last; with an odd count the high half of the last word is zero.

## Decisions (`docs/examples/stream_inband/decisions.md`)

1. **Configuration:** `n` and `k` travel in every COMPUTE header; nothing is configured over AXI-Lite and nothing carries over between commands.
2. **Responses:** COMPUTE gets `RespHdr | y`; END gets nothing; a command that fails in its header gets nothing.
3. **Zero-length data:** `n = 0` is illegal (`BAD_PARAM`), since the spec requires `1 <= n`. `y` is never empty for a legal command (`k <= n`), and is omitted only when an early TLAST leaves no whole window.
4. **Error precedence:** first error in stream order; within a header: TLAST, then unknown command, then invalid parameter. Parameters are checked before anything is written for the command and before the signal is read.
5. **Packing:** as above, by the schemas and `int16_array_utils`; padding halves zero on output and ignored on input.
6. **Rounding / saturation:** none. Max of int16 values is exact; `y` is int16.
7. **`tx_id` in the status:** the failing command's `tx_id` as received (for END, the 0 it carries), 0 if the header ended before word 1, 0 after a clean run.
8. **TLAST on fixed-length bursts:** checked, because the spec requires it: a header must carry TLAST on its last word and nowhere else (`TLAST_EARLY_HDR`, `NO_TLAST_HDR`).
9. **Unused opcode values:** `cmd_type` codes 2..255 are `UNKNOWN_CMD`. The field is a raw uint8 `IntField` so a scenario can serialize such a code; the C++ compares against the generated `MaxpoolCmdType` enum names.
10. **Footer:** none.
