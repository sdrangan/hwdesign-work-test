// fir_body_impl.tpp -- the WHOLE kernel body, hand-written.
//
// Included from gen/fir.hpp.  The generated tops (gen/fir.cpp: fir for 32-bit words,
// fir_bw64 for 64-bit words) hold every interface pragma and call body() with the two
// streams.  One call handles ONE command: header, payload, response.  Everything that must
// outlive a command -- the taps, the delay line, the counters -- is static, so it persists
// from call to call (and each width's instantiation has its own).  The Python twin, bit for
// bit, is fir_command() in fir.py.

#include "include/fir_cmd_hdr.h"
#include "include/fir_resp_hdr.h"
#include "include/fir_resp_ftr.h"
#include "include/fir_status_msg.h"
#include "include/int16_array_utils.h"

namespace fir_impl {

namespace au = int16_array_utils;

// (not "NT": the Windows csim build defines a macro of that name)
static const int NTAPS = 32;       // NTAPS_MAX in fir.py
static const int NH = NTAPS - 1;   // delay line: the previous NTAPS - 1 samples, oldest first
typedef ap_int<16> samp_t;         // Q1.15 sample, tap or output
typedef ap_int<40> acc_t;          // exact: 32 products of two int16 need 37 bits

static const unsigned OP_LOAD_TAPS = 1;
static const unsigned OP_PROCESS = 2;
static const unsigned OP_STATUS = 3;

// sat16((acc + 2^14) >> 15): round half up, arithmetic shift, clip.
static inline samp_t round_sat(acc_t acc) {
#pragma HLS INLINE
    const acc_t r = (acc + acc_t(1 << 14)) >> 15;
    if (r > acc_t(32767)) return samp_t(32767);
    if (r < acc_t(-32768)) return samp_t(-32768);
    return samp_t(r);
}

// Read and drop words through the next TLAST.
template <int bw>
void discard(hls::stream<streamutils::axi4s_word<bw>>& s_in) {
#pragma HLS INLINE
    bool last = false;
DISCARD:
    while (!last) {
#pragma HLS PIPELINE II=1
        streamutils::axi4s_word<bw> w = s_in.read();
        last = w.last;
    }
}

// The LOAD_TAPS payload, into shadow[0..ntaps-1].  Returns the framing error.
template <int bw>
FirError load(hls::stream<streamutils::axi4s_word<bw>>& s_in, samp_t shadow[NTAPS], int ntaps,
              int& nin) {
#pragma HLS INLINE
    const int PF = au::pf<bw>();
    const int nwords = (ntaps + PF - 1) / PF;
    bool got_last = false, early = false;
LOAD:
    for (int w = 0; w < nwords && !got_last; ++w) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=16
        const int nrem = ntaps - w * PF;
        const int c = nrem < PF ? nrem : PF;
        samp_t x[PF];
#pragma HLS ARRAY_PARTITION variable=x complete
        for (int k = 0; k < PF; ++k) x[k] = 0;
        streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;
        au::read_axi4_stream_lane<bw>(s_in, x, c, tl);
        for (int k = 0; k < PF; ++k) {
#pragma HLS UNROLL
            const int idx = w * PF + k;
            if (k < c && idx < NTAPS) shadow[idx] = x[k];
        }
        nin += c;
        if (tl == streamutils::tlast_status::tlast_at_end) {
            got_last = true;
            early = (w + 1 < nwords);
        }
    }
    if (!got_last) {
        discard<bw>(s_in);
        return FirError::NO_TLAST;
    }
    return early ? FirError::TLAST_EARLY : FirError::NO_ERROR;
}

// The PROCESS payload: filter every sample, write y[n] for n % decim == 0, one input word
// per clock.  Returns the framing error.
template <int in_bw, int out_bw>
FirError process(hls::stream<streamutils::axi4s_word<in_bw>>& s_in,
                 hls::stream<streamutils::axi4s_word<out_bw>>& m_out,
                 const samp_t taps[NTAPS], samp_t hist[NH], int nsamp, int decim,
                 int& nin, int& nout) {
#pragma HLS INLINE
    const int PF = au::pf<in_bw>();
    const int nwords = (nsamp + PF - 1) / PF;
    const ap_uint<2> dshift = (decim == 4) ? 2 : (decim == 2) ? 1 : 0;
    const ap_uint<2> dmask = decim - 1;
    const bool d_gt_pf = decim > PF;

    // Outputs waiting for a full word.  A full word is written as soon as it fills -- except
    // when decim > PF (decim 4 at 32 bits), where every other input word adds no output: then
    // a full word waits one input word, so that its TLAST is known when it is written.
    // The loop-carried counters are narrow on purpose: ocnt + added -> write -> ocnt is the
    // one recurrence that must close in a single clock.
    samp_t obuf[PF];
#pragma HLS ARRAY_PARTITION variable=obuf complete
    for (int k = 0; k < PF; ++k) obuf[k] = 0;
    ap_uint<3> ocnt = 0;
    ap_uint<2> phase = 0;                // (index of this word's first sample) mod 4
    ap_uint<16> nrem = nsamp;            // samples not yet read
    bool got_last = false, early = false;

PROCESS:
    for (int w = 0; w < nwords && !got_last; ++w) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=512 avg=256
        const bool word_is_last = nrem <= PF;
        const ap_uint<3> c = word_is_last ? ap_uint<3>(nrem) : ap_uint<3>(PF);
        samp_t x[PF];
#pragma HLS ARRAY_PARTITION variable=x complete
        for (int k = 0; k < PF; ++k) x[k] = 0;
        streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;
        au::read_axi4_stream_lane<in_bw>(s_in, x, c, tl);
        const bool last = (tl == streamutils::tlast_status::tlast_at_end);
        const bool is_final = last || word_is_last;

        // The window: the delay line, then this word's samples.
        samp_t win[NH + PF];
#pragma HLS ARRAY_PARTITION variable=win complete
        for (int t = 0; t < NH; ++t) win[t] = hist[t];
        for (int k = 0; k < PF; ++k) win[NH + k] = x[k];

        // One output per lane: acc = sum_j h[j] x[n - j], exactly.
        samp_t y[PF];
#pragma HLS ARRAY_PARTITION variable=y complete
        for (int k = 0; k < PF; ++k) {
            acc_t acc = 0;
            for (int j = 0; j < NTAPS; ++j) {
                acc += acc_t(taps[j] * win[NH + k - j]);
            }
            y[k] = round_sat(acc);
        }

        // The delay line moves by the c valid samples: hist'[t] = win[t + c].
        for (int t = 0; t < NH; ++t) {
            samp_t v = win[t + 1];
            for (int m = 2; m <= PF; ++m) {
                if (c == m) v = win[t + m];
            }
            hist[t] = v;
        }

        // Keep y[n] for n = w*PF + k with n % decim == 0 (decim divides 4, so n mod 4 will do).
        ap_uint<3> added = 0;
        for (int k = 0; k < PF; ++k) {
            const ap_uint<2> nmod = phase + ap_uint<2>(k);
            if (ap_uint<3>(k) < c && (nmod & dmask) == 0) {
                obuf[ap_uint<3>(ocnt + (ap_uint<3>(k) >> dshift))] = y[k];
                ++added;
            }
        }
        const ap_uint<3> ofill = ocnt + added;
        const bool write = is_final ? (ofill != 0) : (ofill == PF && (!d_gt_pf || added == 0));
        if (write) {
            au::write_axi4_stream_lane<out_bw>(obuf, m_out, is_final, ofill);
            nout += ofill;
            ocnt = 0;
        } else {
            ocnt = ofill;
        }
        nin += c;
        nrem -= c;
        phase += ap_uint<2>(PF & 3);
        if (last) {
            got_last = true;
            early = !word_is_last;
        }
    }
    if (!got_last) {
        discard<in_bw>(s_in);
        return FirError::NO_TLAST;
    }
    return early ? FirError::TLAST_EARLY : FirError::NO_ERROR;
}

// The kernel: one command per call.
template <int in_bw, int out_bw>
void body(hls::stream<streamutils::axi4s_word<in_bw>>& s_in,
          hls::stream<streamutils::axi4s_word<out_bw>>& m_out) {
#pragma HLS INLINE
    // State that persists between commands (zero at power-up).
    static samp_t taps[NTAPS];
#pragma HLS ARRAY_PARTITION variable=taps complete
    static samp_t hist[NH];
#pragma HLS ARRAY_PARTITION variable=hist complete
    static ap_uint<8> ntaps = 0;
    static ap_uint<32> nsamp_total = 0;
    static ap_uint<16> nerr = 0;
    static ap_uint<8> last_error = 0;

    FirCmdHdr hdr;
    hdr.read_axi4_stream<in_bw>(s_in);

    FirRespHdr resp;
    resp.tx_id = hdr.tx_id;
    resp.opcode = hdr.opcode;
    resp.write_axi4_stream<out_bw>(m_out, true);

    const unsigned op = hdr.opcode;
    const int cnt = hdr.count;
    const int decim = hdr.decim;
    FirError err = FirError::NO_ERROR;
    int nin = 0, nout = 0;

    if (op == OP_LOAD_TAPS) {
        if (cnt == 0 || cnt > NTAPS) {
            err = FirError::BAD_NTAPS;
            if (cnt != 0) discard<in_bw>(s_in);
        } else {
            samp_t shadow[NTAPS];
#pragma HLS ARRAY_PARTITION variable=shadow complete
            for (int j = 0; j < NTAPS; ++j) shadow[j] = 0;
            err = load<in_bw>(s_in, shadow, cnt, nin);
            if (err == FirError::NO_ERROR) {
                for (int j = 0; j < NTAPS; ++j) taps[j] = shadow[j];
                for (int t = 0; t < NH; ++t) hist[t] = 0;
                ntaps = cnt;
                nsamp_total = 0;
            }
        }
    } else if (op == OP_PROCESS) {
        if (ntaps == 0) {
            err = FirError::NO_TAPS;
        } else if (!(decim == 1 || decim == 2 || decim == 4) || (cnt & (decim - 1)) != 0) {
            err = FirError::BAD_DECIM;
        }
        if (err != FirError::NO_ERROR) {
            if (cnt != 0) discard<in_bw>(s_in);
        } else {
            if (hdr.reset) {
                for (int t = 0; t < NH; ++t) hist[t] = 0;
            }
            if (cnt != 0) {
                err = process<in_bw, out_bw>(s_in, m_out, taps, hist, cnt, decim, nin, nout);
                nsamp_total += nin;
            }
        }
    } else if (op == OP_STATUS) {
        FirStatusMsg msg;
        msg.ntaps = ntaps;
        msg.nsamp_total = nsamp_total;
        msg.nerr = nerr;
        msg.last_error = static_cast<FirError>(static_cast<unsigned int>(last_error));
        msg.write_axi4_stream<out_bw>(m_out, true);
    } else {
        err = FirError::BAD_OPCODE;
    }

    if (err != FirError::NO_ERROR) {
        if (nerr != ap_uint<16>(0xFFFF)) nerr = nerr + 1;
        last_error = static_cast<unsigned int>(err);
        for (int t = 0; t < NH; ++t) hist[t] = 0;
    }

    FirRespFtr ftr;
    ftr.nin = nin;
    ftr.nout = nout;
    ftr.error = err;
    ftr.write_axi4_stream<out_bw>(m_out, true);
}

}  // namespace fir_impl
