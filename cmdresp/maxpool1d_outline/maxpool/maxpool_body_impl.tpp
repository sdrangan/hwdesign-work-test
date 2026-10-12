// maxpool_body_impl.tpp -- the WHOLE kernel body, hand-written.
//
// Included from gen/maxpool.hpp.  The generated top (gen/maxpool.cpp) holds every interface
// pragma and calls body() with every kernel argument; the status registers are references,
// so this code writes halted / error / tx_id itself.  The Python twin, word for word, is
// maxpool_stream_model() in maxpool.py.
//
// The contract (docs/examples/stream_inband/index.md) in the code:
//   rule 2  everything the kernel computes with arrives on s_in: each COMPUTE header carries
//           its own n and k, and nothing is kept between commands (rule 4);
//   rule 5  the status is cleared at the start of every activation;
//   rule 6  on an error: set the status, close the output burst with TLAST, and return
//           without reading anything more from s_in.
// Added by this spec: the header's TLAST is checked, then its command code, then n and k --
// all before anything is written for the command.

#include "include/int16_array_utils.h"
#include "include/maxpool_cmd_hdr.h"
#include "include/maxpool_cmd_type.h"
#include "include/maxpool_resp_hdr.h"

namespace maxpool_impl {

namespace au = int16_array_utils;

static const int N_MAX = 128;
static const int K_MAX = 8;

static inline ap_uint<8> code(MaxpoolError e) {
#pragma HLS INLINE
    return (ap_uint<8>)static_cast<unsigned int>(e);
}

static inline bool is_cmd(const ap_uint<8>& c, MaxpoolCmdType t) {
#pragma HLS INLINE
    return c == static_cast<unsigned int>(t);
}

// One accepted COMPUTE: the response header, then y.  Returns the error code.
//
// One input word (two samples) per iteration at II=1.  Each sample updates a running max;
// when a window of k fills, its max goes into the output lane buffer y_word.  A full y_word
// is not written at once but held: the previous held word is written (no TLAST) and the new
// one takes its place.  So when the signal ends -- normally, early or without TLAST -- the
// last y word is still in hand and can carry TLAST, even if no window completed in the
// final input word.  At most one output word is written per iteration.
template <int in_bw, int out_bw>
ap_uint<8> transaction(const MaxpoolCmdHdr& cmd_hdr,
                       hls::stream<streamutils::axi4s_word<in_bw>>& s_in,
                       hls::stream<streamutils::axi4s_word<out_bw>>& m_out) {
#pragma HLS INLINE
    MaxpoolRespHdr resp_hdr;
    resp_hdr.tx_id = cmd_hdr.tx_id;
    resp_hdr.write_axi4_stream<out_bw>(m_out, true);

    static const int pf = au::pf<in_bw>();
    const int n = cmd_hdr.n;
    const ap_uint<4> k = cmd_hdr.k;
    const int nwords = (n + pf - 1) / pf;

    au::value_type x_lane[pf];
    au::value_type y_word[pf];
    au::value_type held[pf];
#pragma HLS ARRAY_PARTITION variable=x_lane complete dim=1
#pragma HLS ARRAY_PARTITION variable=y_word complete dim=1
#pragma HLS ARRAY_PARTITION variable=held complete dim=1
    for (int l = 0; l < pf; ++l) {
#pragma HLS UNROLL
        y_word[l] = 0;
        held[l] = 0;
    }

    au::value_type acc = 0;     // running max of the current window
    ap_uint<4> a = 0;           // samples of the current window seen so far
    ap_uint<2> ny = 0;          // results waiting in y_word
    bool held_valid = false;
    bool early = false;
    bool missing = false;

    for (int i = 0; i < nwords; ++i) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=64
        const int nrem = n - i * pf;
        streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;
        au::read_axi4_stream_lane<in_bw>(s_in, x_lane, nrem, tl);
        const bool in_tlast = (tl == streamutils::tlast_status::tlast_at_end);
        const bool final_word = (i == nwords - 1);

        for (int l = 0; l < pf; ++l) {
#pragma HLS UNROLL
            if (l < nrem) {       // the padding half of an odd n's last word is ignored
                const au::value_type v = x_lane[l];
                acc = (a == 0 || v > acc) ? v : acc;
                a = a + 1;
                if (a == k) {
                    y_word[ny] = acc;
                    ny = ny + 1;
                    a = 0;
                }
            }
        }
        if (ny == pf) {
            if (held_valid) {
                au::write_axi4_stream_lane<out_bw>(held, m_out, false, pf);
            }
            for (int l = 0; l < pf; ++l) {
#pragma HLS UNROLL
                held[l] = y_word[l];
            }
            held_valid = true;
            ny = 0;
        }
        if (in_tlast && !final_word) {
            early = true;
            break;
        }
        if (final_word && !in_tlast) {
            missing = true;
        }
    }

    // Flush: the last y word carries TLAST.  No word at all when no window completed.
    if (ny > 0) {
        if (held_valid) {
            au::write_axi4_stream_lane<out_bw>(held, m_out, false, pf);
        }
        au::write_axi4_stream_lane<out_bw>(y_word, m_out, true, (int)ny);
    } else if (held_valid) {
        au::write_axi4_stream_lane<out_bw>(held, m_out, true, pf);
    }

    if (early) return code(MaxpoolError::TLAST_EARLY_SAMP_IN);
    if (missing) return code(MaxpoolError::NO_TLAST_SAMP_IN);
    return code(MaxpoolError::NO_ERROR);
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
    while (true) {
        // Zeroed first: a field whose word never arrived (early TLAST) reads as 0.
        MaxpoolCmdHdr cmd_hdr;
        cmd_hdr.cmd_type = 0;
        cmd_hdr.n = 0;
        cmd_hdr.k = 0;
        cmd_hdr.tx_id = 0;
        streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;
        cmd_hdr.read_axi4_stream<in_bw>(s_in, tl);   // stops at an early TLAST

        ap_uint<8> err = code(MaxpoolError::NO_ERROR);
        if (tl == streamutils::tlast_status::tlast_early) {
            err = code(MaxpoolError::TLAST_EARLY_HDR);
        } else if (tl != streamutils::tlast_status::tlast_at_end) {
            err = code(MaxpoolError::NO_TLAST_HDR);
        } else if (!is_cmd(cmd_hdr.cmd_type, MaxpoolCmdType::COMPUTE) &&
                   !is_cmd(cmd_hdr.cmd_type, MaxpoolCmdType::END)) {
            err = code(MaxpoolError::UNKNOWN_CMD);
        } else if (is_cmd(cmd_hdr.cmd_type, MaxpoolCmdType::END)) {
            return;
        } else {
            const ap_uint<16> n = cmd_hdr.n;
            const ap_uint<16> k = cmd_hdr.k;
            const ap_uint<16> kmax = (n < K_MAX) ? n : (ap_uint<16>)K_MAX;
            if (n < 1 || n > N_MAX || k < 1 || k > kmax) {
                err = code(MaxpoolError::BAD_PARAM);
            } else {
                err = transaction<in_bw, out_bw>(cmd_hdr, s_in, m_out);
            }
        }
        if (err != code(MaxpoolError::NO_ERROR)) {   // rule 6: any burst is closed; stop here
            error = err;
            tx_id = cmd_hdr.tx_id;
            halted = 1;
            return;
        }
    }
}

}  // namespace maxpool_impl
