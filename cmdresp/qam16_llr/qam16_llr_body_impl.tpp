// qam16_llr_body_impl.tpp -- the WHOLE kernel body, hand-written.
//
// Included from gen/qam16_llr.hpp.  The generated top (gen/qam16_llr.cpp) holds every
// interface pragma and calls body() with every kernel argument; the status registers are
// references, so this code writes halted / error / tx_id itself.
// The Python twin, bit for bit, is qam16_llr_stream_model() in qam16_llr.py.
//
// The contract (docs/examples/stream_inband/index.md) in the code:
//   rule 2  everything the kernel computes with arrives on s_in: each DEMAP header carries
//           its own nsamp and inv_var, and nothing is kept between commands (rule 4);
//   rule 5  the status is cleared at the start of every activation;
//   rule 6  on an error: set the status, close the output burst with TLAST, and return
//           without reading anything more from s_in.

#include "include/complex__int16_array_utils.h"
#include "include/int8_array_utils.h"
#include "include/qam16_llr_cmd_hdr.h"
#include "include/qam16_llr_cmd_type.h"
#include "include/qam16_llr_resp_hdr.h"

namespace qam16_llr_impl {

namespace cau = complex__int16_array_utils;   // one complex int16 sample per 32-bit word
namespace lau = int8_array_utils;             // four int8 LLRs per 32-bit word

static const int NSAMP_MAX = 1024;

// sat8((p + 2^13) >> 14): >> on a signed ap_int is arithmetic, so this rounds half up.
static inline lau::value_type round_sat(ap_int<35> p) {
#pragma HLS INLINE
    const ap_int<36> r = (p + (ap_int<36>)8192) >> 14;
    if (r > 127) return (lau::value_type)127;
    if (r < -128) return (lau::value_type)(-128);
    return (lau::value_type)r;
}

// One axis: the sign LLR inv_var*y and the magnitude LLR inv_var*(2 - |y|), as
// qam16_llr_eval(): |y| on 17 bits (|-32768| = 32768), 512 - |y| on 18 bits signed, and
// exact products before the rounding.
static inline void demap_axis(ap_uint<16> inv_var, ap_int<16> y,
                              lau::value_type& llr_sign, lau::value_type& llr_mag) {
#pragma HLS INLINE
    const ap_int<17> iv = inv_var;
    const ap_int<17> ys = y;
    const ap_uint<17> a = (ys < 0) ? (ap_uint<17>)(-ys) : (ap_uint<17>)ys;
    const ap_int<18> d = (ap_int<18>)512 - (ap_int<18>)a;
    const ap_int<33> p = iv * y;
    const ap_int<35> q = iv * d;
    llr_sign = round_sat(p);
    llr_mag = round_sat(q);
}

static inline ap_uint<8> code(Qam16LlrError e) {
#pragma HLS INLINE
    return (ap_uint<8>)static_cast<unsigned int>(e);
}

// One DEMAP command whose parameters were checked: the response header, then a word of four
// LLRs per sample word.  Returns the error code.
template <int in_bw, int out_bw>
ap_uint<8> transaction(const Qam16LlrCmdHdr& cmd_hdr,
                       hls::stream<streamutils::axi4s_word<in_bw>>& s_in,
                       hls::stream<streamutils::axi4s_word<out_bw>>& m_out) {
#pragma HLS INLINE
    Qam16LlrRespHdr resp_hdr;
    resp_hdr.tx_id = cmd_hdr.tx_id;
    resp_hdr.write_axi4_stream<out_bw>(m_out, true);

    const ap_uint<16> inv_var = cmd_hdr.inv_var;
    const ap_uint<11> nsamp = cmd_hdr.nsamp;     // checked: 1..1024
    ap_uint<8> err = code(Qam16LlrError::NO_ERROR);

    // A sample per iteration: read its word, demap, write the LLR word.  The output word gets
    // TLAST when it completes nsamp -- or when the input word had TLAST early, so a burst that
    // ends early is still closed (rule 6).
    samples:
    for (ap_uint<11> i = 0; i < nsamp; ++i) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=1024
        cau::value_type x[cau::lane_capacity<in_bw>()];
        lau::value_type llr[lau::lane_capacity<out_bw>()];
#pragma HLS ARRAY_PARTITION variable=x complete dim=1
#pragma HLS ARRAY_PARTITION variable=llr complete dim=1
        streamutils::tlast_status lane_tlast = streamutils::tlast_status::no_tlast;
        cau::read_axi4_stream_lane<in_bw>(s_in, x, 1, lane_tlast);
        const bool in_tlast = (lane_tlast == streamutils::tlast_status::tlast_at_end);
        const bool final_word = (i == nsamp - 1);
        demap_axis(inv_var, x[0].re, llr[0], llr[1]);
        demap_axis(inv_var, x[0].im, llr[2], llr[3]);
        lau::write_axi4_stream_lane<out_bw>(llr, m_out, final_word || in_tlast, 4);
        if (in_tlast && !final_word) {
            err = code(Qam16LlrError::TLAST_EARLY_SAMP_IN);
            break;
        }
        if (final_word && !in_tlast) {
            err = code(Qam16LlrError::NO_TLAST_SAMP_IN);
        }
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
    error = code(Qam16LlrError::NO_ERROR);
    tx_id = 0;
    commands:
    while (true) {
        Qam16LlrCmdHdr cmd_hdr;
        cmd_hdr.read_axi4_stream<in_bw>(s_in);
        if (cmd_hdr.cmd_type == (unsigned)Qam16LlrCmdType::END) {
            return;
        }
        // The header's checks, before anything is written or any sample read for the command.
        ap_uint<8> err = code(Qam16LlrError::NO_ERROR);
        if (cmd_hdr.cmd_type != (unsigned)Qam16LlrCmdType::DEMAP) {
            err = code(Qam16LlrError::UNKNOWN_CMD);
        } else if (cmd_hdr.nsamp < 1 || cmd_hdr.nsamp > NSAMP_MAX) {
            err = code(Qam16LlrError::BAD_PARAM);
        } else {
            err = transaction<in_bw, out_bw>(cmd_hdr, s_in, m_out);
        }
        if (err != code(Qam16LlrError::NO_ERROR)) {   // rule 6: the burst is closed; stop here
            error = err;
            tx_id = cmd_hdr.tx_id;
            halted = 1;
            return;
        }
    }
}

}  // namespace qam16_llr_impl
