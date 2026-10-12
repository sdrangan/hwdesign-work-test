# Wire layout

Every row below was obtained by serializing an instance with Waveflow (32-bit words) and
reading the result:

```
MaxpoolCmdHdr(cmd_type=0xAB, tx_id=0x1234, n=0x0080, k=0x08).serialize(32) -> [0x001234ab, 0x00080080]
MaxpoolCmdHdr(cmd_type=1 (END), tx_id=0, n=0, k=0).serialize(32)         -> [0x00000001, 0x00000000]
MaxpoolRespHdr(tx_id=0x1234).serialize(32)                                -> [0x00001234]
write_array(int16 [1, -2, 3], Int16, word_bw=32)                          -> [0xfffe0001, 0x00000003]
write_array(int16 [0x1111, 0x2222, -1, -32768], Int16, word_bw=32)        -> [0x22221111, 0x8000ffff]
```

## Command header (2 words, its own burst, TLAST on word 1)

| Word | Bits | Field | Type |
| --- | --- | --- | --- |
| 0 | 7:0 | `cmd_type` | uint8: COMPUTE = 0, END = 1, any other value is unknown |
| 0 | 23:8 | `tx_id` | uint16 |
| 0 | 31:24 | -- | zero |
| 1 | 15:0 | `n` | uint16 (legal 1..128; 0 on END) |
| 1 | 23:16 | `k` | uint8 (legal 1..min(n, 8); 0 on END) |
| 1 | 31:24 | -- | zero |

## Response header (1 word, its own burst, TLAST on it)

| Word | Bits | Field | Type |
| --- | --- | --- | --- |
| 0 | 15:0 | `tx_id` | uint16, echo of the command's |
| 0 | 31:16 | -- | zero |

## Response footer

None (prompt.md defines no per-command result known only after the data).

## Sample bursts (signal in, y out)

Two int16 per 32-bit word, element `2j` in bits 15:0 and element `2j+1` in bits 31:16 of word
`j` (two's complement).  A signal of `n` samples is `ceil(n/2)` words, TLAST on the last; an
odd `n` leaves bits 31:16 of the last word zero, and the kernel ignores them.  The output `y`
of `m = floor(n/k)` results is packed the same way, `ceil(m/2)` words, TLAST on the last; an
odd `m` leaves the high half of the last word zero.  Since `1 <= k <= n` for a legal command,
`m >= 1` and the output burst is never empty.

## Decisions (docs/examples/stream_inband/decisions.md)

1. **Configuration**: `n` and `k` travel in every COMPUTE header; nothing persists between
   commands; AXI-Lite holds only `halted`, `error`, `tx_id`.
2. **Responses**: COMPUTE gets `RespHdr | y`; END gets nothing; a failed command gets only
   what was written before the error (nothing at all for header and parameter errors).
3. **Zero-length data**: `n = 0` is illegal (prompt: `1 <= n`), so it is `BAD_PARAM`; with a
   legal command `m >= 1`, so the y burst is never empty.  An early signal TLAST that
   completes no window leaves no y burst (the response header alone, already closed).
4. **Error precedence**: first detected in stream order, and it ends the run.  Within a
   header: header framing (`TLAST_EARLY_HDR` as soon as TLAST arrives before word 1,
   `NO_TLAST_HDR` when word 1 has none), then `UNKNOWN_CMD`, then `BAD_PARAM` (n checked with
   k, one code).  So an END header without TLAST is `NO_TLAST_HDR`, not a clean end.
   `BAD_PARAM` is detected before anything is written for the command and before its signal
   is read.  The two signal framing errors cannot both happen in one burst.
5. **Packing**: by the schemas (`serialize()`) and the Int16 array utilities, as above.
   Unused bits are zero when sent and ignored when received.
6. **Rounding and saturation**: none.  The max of int16 values is an int16 value; the
   comparison is signed.
7. **`tx_id` in the status**: the `tx_id` field of the command that failed, as received.  For
   `TLAST_EARLY_HDR` only word 0 arrived, which holds `tx_id`.  For an END header without TLAST
   it is END's `tx_id` (sent as 0).  0 after a successful run.
8. **TLAST on fixed-length bursts**: checked.  Unlike the reference, prompt.md makes an early
   or missing TLAST on a *header* an error, so each command header is its own burst with TLAST
   on its last word, read with the generated `read_axi4_stream(s, tlast_status)`.  The
   response header is likewise its own burst, TLAST on it.
9. **Unused opcodes**: `cmd_type` is an 8-bit integer field; 2..255 are `UNKNOWN_CMD`.  It is
   an `IntField`, not an `EnumField`, because a Waveflow enum refuses to serialize an
   out-of-range value, so no scenario could send one without hand-packing.
10. **Footer**: none.

Signal-burst errors (frame F4, adapted to a pooling function):

- **Early TLAST on signal word `j`**: the samples of words `0..j` are read (both halves; the
  burst ends on a word boundary), every *whole* window among them is answered, the y burst is
  closed with TLAST on its last word, and the kernel halts with `TLAST_EARLY_SAMP_IN`.
- **Missing TLAST** on the last signal word: all `floor(n/k)` results, TLAST on the last
  word, then halt with `NO_TLAST_SAMP_IN`.
