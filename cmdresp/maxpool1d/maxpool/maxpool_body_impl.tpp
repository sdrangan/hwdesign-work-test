// maxpool_body_impl.tpp -- the WHOLE kernel body, hand-written.
//
// Included from gen/maxpool.hpp.  The generated top (gen/maxpool.cpp: maxpool, 32-bit words)
// holds every interface pragma and calls body() with every kernel argument; the status
// registers are references, so this code writes halted / error / tx_id itself.
// The Python twin, word for word, is maxpool_stream_model() in maxpool.py.
//
// The contract (docs/examples/stream_inband/index.md) in the code:
//   rule 2  everything the kernel computes with arrives on s_in: each COMPUTE header carries
//           its own n and k, and nothing is kept between commands (rule 4);
//   rule 5  the status is cleared at the start of every activation;
//   rule 6  on an error: set the status, close the output burst with TLAST, and return
//           without reading anything more from s_in.

#include "include/int16_array_utils.h"
#include "include/maxpool_cmd_hdr.h"
#include "include/maxpool_resp_hdr.h"

namespace maxpool_impl {

namespace iu = int16_array_utils;

// The command codes and limits of maxpool.py (MaxpoolCmdType, N_MAX, K_MAX).  cmd_type is a
// plain 8-bit field, so these are constants rather than a generated enum.
static const int CMD_COMPUTE = 0;
static const int CMD_END = 1;
static const int N_MAX = 128;
static const int K_MAX = 8;

static inline ap_uint<8> code(MaxpoolError e) {
#pragma HLS INLINE
    return (ap_uint<8>)static_cast<unsigned int>(e);
}

// One legal COMPUTE command: the response header, then y.  Returns the error code.
//
// One signal word -- both of its samples -- per iteration (the lane loop's shape A): the prompt
// asks for one input word per clock.  The two samples go through two chained compares in the
// same iteration.  The running maximum restarts with the first sample of each window; a result
// is produced when the k-th sample arrives, so one word completes 0, 1 or (k = 1) 2 windows.
// Results are packed two to a word, so an iteration fills at most one output word, and the loop
// writes at most one word per iteration.  A full output word is held back one step (`pend`), so
// that whichever word turns out to be the last -- at the end of the signal or at an early TLAST
// -- is written with TLAST (rule 6).
template <int in_bw, int out_bw>
ap_uint<8> compute(const MaxpoolCmdHdr& cmd_hdr,
                   hls::stream<streamutils::axi4s_word<in_bw>>& s_in,
                   hls::stream<streamutils::axi4s_word<out_bw>>& m_out) {
#pragma HLS INLINE
    MaxpoolRespHdr resp_hdr;
    resp_hdr.tx_id = cmd_hdr.tx_id;
    resp_hdr.write_axi4_stream<out_bw>(m_out, true);

    static const int PF = iu::lane_capacity<in_bw>();
    static const int PFO = iu::lane_capacity<out_bw>();
    static_assert(PF == 2 && PFO == 2, "the loop below handles exactly two samples per word");
    const int n = cmd_hdr.n;
    const int k = cmd_hdr.k;
    const int nwords = (n + PF - 1) / PF;

    iu::value_type x_lane[PF];     // the signal word being consumed
    iu::value_type p_lane[PFO];    // the full output word held back
    iu::value_type w_lane[PFO];    // the output word completed in this iteration
#pragma HLS ARRAY_PARTITION variable=x_lane complete dim=1
#pragma HLS ARRAY_PARTITION variable=p_lane complete dim=1
#pragma HLS ARRAY_PARTITION variable=w_lane complete dim=1
    for (int j = 0; j < PFO; ++j) {
#pragma HLS UNROLL
        p_lane[j] = 0;
        w_lane[j] = 0;
    }

    iu::value_type acc = 0;
    iu::value_type y_half = 0;    // a result waiting for its partner in the output word
    int a = 0;                    // position in the current window
    bool y_has = false;           // y_half is valid
    bool pend = false;            // p_lane holds a full word not yet written
    int p_cnt = PFO;              // results in the word to write at the end
    ap_uint<8> err = code(MaxpoolError::NO_ERROR);

WORDS:
    for (int i = 0; i < nwords; ++i) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=64
        const int nrem = n - i * PF;
        const bool has1 = nrem >= PF;             // the word's second sample is part of x
        streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;
        iu::read_axi4_stream_lane<in_bw>(s_in, x_lane, has1 ? PF : 1, tl);
        const bool in_tlast = (tl == streamutils::tlast_status::tlast_at_end);

        // Sample 0, then sample 1, of the word.
        const iu::value_type v0 = x_lane[0];
        const iu::value_type v1 = x_lane[1];
        const iu::value_type r0 = (a == 0 || v0 > acc) ? v0 : acc;
        const bool d0 = (a == k - 1);
        const int a1 = d0 ? 0 : a + 1;
        const iu::value_type r1 = (a1 == 0 || v1 > r0) ? v1 : r0;
        const bool d1 = has1 && (a1 == k - 1);
        a = has1 ? (d1 ? 0 : a1 + 1) : a1;
        acc = has1 ? r1 : r0;

        // The results of this word, in order, joined to any half-filled output word.
        const iu::value_type first = d0 ? r0 : r1;
        bool full = false;
        if (d0 && d1) {
            w_lane[0] = y_has ? y_half : r0;
            w_lane[1] = y_has ? r0 : r1;
            y_half = r1;                          // kept only if y_has
            full = true;
        } else if (d0 || d1) {
            w_lane[0] = y_half;
            w_lane[1] = first;
            full = y_has;
            y_half = y_has ? y_half : first;
            y_has = !y_has;
        }
        if (full) {
            if (pend) {
                iu::write_axi4_stream_lane<out_bw>(p_lane, m_out, false, PFO);
            }
            for (int j = 0; j < PFO; ++j) {
#pragma HLS UNROLL
                p_lane[j] = w_lane[j];
            }
            pend = true;
        }

        const bool final_word = (i == nwords - 1);
        if (in_tlast && !final_word) {            // TLAST before the last signal word
            err = code(MaxpoolError::TLAST_EARLY_SAMP_IN);
            break;
        }
        if (final_word && !in_tlast) {            // the last signal word had no TLAST
            err = code(MaxpoolError::NO_TLAST_SAMP_IN);
        }
    }

    // Flush: a partly filled word becomes the held-back one, and the held-back word -- the
    // last of the burst -- goes out with TLAST.
    if (y_has) {
        if (pend) {
            iu::write_axi4_stream_lane<out_bw>(p_lane, m_out, false, PFO);
        }
        p_lane[0] = y_half;
        p_cnt = 1;
        pend = true;
    }
    if (pend) {
        iu::write_axi4_stream_lane<out_bw>(p_lane, m_out, true, p_cnt);
    }
    return err;
}

// The kernel: one activation, a loop over in-band commands until END or an error.
template <int in_bw, int out_bw>
void body(hls::stream<streamutils::axi4s_word<in_bw>>& s_in,
          hls::stream<streamutils::axi4s_word<out_bw>>& m_out,
          ap_uint<1>& halted, ap_uint<8>& error, ap_uint<16>& tx_id) {
#pragma HLS INLINE
    halted = 0;                                   // rule 5
    error = code(MaxpoolError::NO_ERROR);
    tx_id = 0;
COMMANDS:
    while (true) {
        MaxpoolCmdHdr cmd_hdr;
        streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;
        cmd_hdr.read_axi4_stream<in_bw>(s_in, tl);  // stops at an early TLAST

        ap_uint<8> err = code(MaxpoolError::NO_ERROR);
        if (tl == streamutils::tlast_status::tlast_early) {
            err = code(MaxpoolError::TLAST_EARLY_HDR);
        } else if (tl != streamutils::tlast_status::tlast_at_end) {
            err = code(MaxpoolError::NO_TLAST_HDR);
        } else if (cmd_hdr.cmd_type == CMD_END) {
            return;
        } else if (cmd_hdr.cmd_type != CMD_COMPUTE) {
            err = code(MaxpoolError::UNKNOWN_CMD);
        } else if (cmd_hdr.n < 1 || cmd_hdr.n > N_MAX || cmd_hdr.k < 1 || cmd_hdr.k > K_MAX ||
                   cmd_hdr.k > cmd_hdr.n) {
            err = code(MaxpoolError::BAD_PARAM);  // before anything is written or read
        } else {
            err = compute<in_bw, out_bw>(cmd_hdr, s_in, m_out);
        }
        if (err != code(MaxpoolError::NO_ERROR)) {  // rule 6: any burst is closed; stop here
            error = err;
            tx_id = cmd_hdr.tx_id;
            halted = 1;
            return;
        }
    }
}

}  // namespace maxpool_impl
