#ifndef INCLUDE_FIR_STATUS_MSG_H
#define INCLUDE_FIR_STATUS_MSG_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

#include "fir_error.h"

struct FirStatusMsg {
    ap_uint<8> ntaps;  // Loaded taps, 0 if none
    ap_uint<32> nsamp_total;  // Samples filtered since the last successful LOAD_TAPS
    ap_uint<16> nerr;  // Errors since power-up, saturating
    FirError last_error;  // Most recent error

    static constexpr int bitwidth = 59;

    template<int word_bw>
    struct word_bw_tag {};

    template<int word_bw>
    static constexpr int nwords_value(word_bw_tag<word_bw>) {
            static_assert(word_bw < 0, "Unsupported word_bw for nwords");
            return 0;
    }

    static constexpr int nwords_value(word_bw_tag<32>) {
            return 3;
    }

    static constexpr int nwords_value(word_bw_tag<64>) {
            return 1;
    }

    template<int word_bw>
    static constexpr int nwords() {
        return nwords_value(word_bw_tag<word_bw>{});
    }

    static ap_uint<bitwidth> pack_to_uint(const FirStatusMsg& data) {
        ap_uint<bitwidth> res = 0;
        res.range(7, 0) = data.ntaps;
        res.range(39, 8) = data.nsamp_total;
        res.range(55, 40) = data.nerr;
        res.range(58, 56) = (ap_uint<3>)(static_cast<unsigned int>(data.last_error));
        return res;
    }

    static FirStatusMsg unpack_from_uint(const ap_uint<bitwidth>& packed) {
        FirStatusMsg data;
        data.ntaps = (ap_uint<8>)(packed.range(7, 0));
        data.nsamp_total = (ap_uint<32>)(packed.range(39, 8));
        data.nerr = (ap_uint<16>)(packed.range(55, 40));
        data.last_error = static_cast<FirError>(static_cast<unsigned int>(packed.range(58, 56)));
        return data;
    }

    template<int word_bw>
    static void write_array_impl(word_bw_tag<word_bw>, const FirStatusMsg* self, ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_array");
        (void)self;
        (void)x;
    }

    static void write_array_impl(word_bw_tag<32>, const FirStatusMsg* self, ap_uint<32> x[]) {
        x[0] = 0;
        x[0].range(7, 0) = self->ntaps;
        x[1] = self->nsamp_total;
        x[2] = 0;
        x[2].range(15, 0) = self->nerr;
        x[2].range(18, 16) = (ap_uint<3>)(static_cast<unsigned int>(self->last_error));
    }

    static void write_array_impl(word_bw_tag<64>, const FirStatusMsg* self, ap_uint<64> x[]) {
        x[0] = 0;
        x[0].range(7, 0) = self->ntaps;
        x[0].range(39, 8) = self->nsamp_total;
        x[0].range(55, 40) = self->nerr;
        x[0].range(58, 56) = (ap_uint<3>)(static_cast<unsigned int>(self->last_error));
    }

    template<int word_bw>
    void write_array(ap_uint<word_bw> x[]) const {
        write_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void write_stream_impl(word_bw_tag<word_bw>, const FirStatusMsg* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_stream");
        (void)self;
        (void)s;
    }

    static void write_stream_impl(word_bw_tag<32>, const FirStatusMsg* self, hls::stream<ap_uint<32>> &s) {
            ap_uint<32> w = 0;
        w.range(7, 0) = self->ntaps;
        s.write(w);
        w = 0;
        w = self->nsamp_total;
        s.write(w);
        w = 0;
        w.range(15, 0) = self->nerr;
        w.range(18, 16) = (ap_uint<3>)(static_cast<unsigned int>(self->last_error));
        s.write(w);
    }

    static void write_stream_impl(word_bw_tag<64>, const FirStatusMsg* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w.range(7, 0) = self->ntaps;
        w.range(39, 8) = self->nsamp_total;
        w.range(55, 40) = self->nerr;
        w.range(58, 56) = (ap_uint<3>)(static_cast<unsigned int>(self->last_error));
        s.write(w);
    }

    template<int word_bw>
    void write_stream(hls::stream<ap_uint<word_bw>> &s) const {
        write_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void write_axi4_stream_impl(word_bw_tag<word_bw>, const FirStatusMsg* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_axi4_stream");
        (void)self;
        (void)s;
        (void)tlast;
    }

    static void write_axi4_stream_impl(word_bw_tag<32>, const FirStatusMsg* self, hls::stream<streamutils::axi4s_word<32>> &s, bool tlast) {
            ap_uint<32> w = 0;
        w.range(7, 0) = self->ntaps;
        streamutils::write_axi4_word<32>(s, w, false);
        w = 0;
        w = self->nsamp_total;
        streamutils::write_axi4_word<32>(s, w, false);
        w = 0;
        w.range(15, 0) = self->nerr;
        w.range(18, 16) = (ap_uint<3>)(static_cast<unsigned int>(self->last_error));
        streamutils::write_axi4_word<32>(s, w, tlast);
    }

    static void write_axi4_stream_impl(word_bw_tag<64>, const FirStatusMsg* self, hls::stream<streamutils::axi4s_word<64>> &s, bool tlast) {
            ap_uint<64> w = 0;
        w.range(7, 0) = self->ntaps;
        w.range(39, 8) = self->nsamp_total;
        w.range(55, 40) = self->nerr;
        w.range(58, 56) = (ap_uint<3>)(static_cast<unsigned int>(self->last_error));
        streamutils::write_axi4_word<64>(s, w, tlast);
    }

    template<int word_bw>
    void write_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast = true) const {
        write_axi4_stream_impl(word_bw_tag<word_bw>{}, this, s, tlast);
    }

    template<int word_bw>
    static void read_array_impl(word_bw_tag<word_bw>, FirStatusMsg* self, const ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_array");
        (void)self;
        (void)x;
    }

    static void read_array_impl(word_bw_tag<32>, FirStatusMsg* self, const ap_uint<32> x[]) {
        self->ntaps = (ap_uint<8>)(x[0].range(7, 0));
        self->nsamp_total = (ap_uint<32>)(x[1]);
        self->nerr = (ap_uint<16>)(x[2].range(15, 0));
        self->last_error = static_cast<FirError>(static_cast<unsigned int>(x[2].range(18, 16)));
    }

    static void read_array_impl(word_bw_tag<64>, FirStatusMsg* self, const ap_uint<64> x[]) {
        self->ntaps = (ap_uint<8>)(x[0].range(7, 0));
        self->nsamp_total = (ap_uint<32>)(x[0].range(39, 8));
        self->nerr = (ap_uint<16>)(x[0].range(55, 40));
        self->last_error = static_cast<FirError>(static_cast<unsigned int>(x[0].range(58, 56)));
    }

    template<int word_bw>
    void read_array(const ap_uint<word_bw> x[]) {
        read_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void read_stream_impl(word_bw_tag<word_bw>, FirStatusMsg* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_stream");
        (void)self;
        (void)s;
    }

    static void read_stream_impl(word_bw_tag<32>, FirStatusMsg* self, hls::stream<ap_uint<32>> &s) {
            ap_uint<32> w = 0;
        w = s.read();
        self->ntaps = (ap_uint<8>)(w.range(7, 0));
        w = s.read();
        self->nsamp_total = (ap_uint<32>)(w);
        w = s.read();
        self->nerr = (ap_uint<16>)(w.range(15, 0));
        self->last_error = static_cast<FirError>(static_cast<unsigned int>(w.range(18, 16)));
    }

    static void read_stream_impl(word_bw_tag<64>, FirStatusMsg* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w = s.read();
        self->ntaps = (ap_uint<8>)(w.range(7, 0));
        self->nsamp_total = (ap_uint<32>)(w.range(39, 8));
        self->nerr = (ap_uint<16>)(w.range(55, 40));
        self->last_error = static_cast<FirError>(static_cast<unsigned int>(w.range(58, 56)));
    }

    template<int word_bw>
    void read_stream(hls::stream<ap_uint<word_bw>> &s) {
        read_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void read_axi4_stream_impl(word_bw_tag<word_bw>, FirStatusMsg* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, streamutils::tlast_status &tl) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_axi4_stream");
        (void)self;
        (void)s;
        (void)tl;
    }

    static void read_axi4_stream_impl(word_bw_tag<32>, FirStatusMsg* self, hls::stream<streamutils::axi4s_word<32>> &s, streamutils::tlast_status &tl) {
            ap_uint<32> w = 0;
            tl = streamutils::tlast_status::no_tlast;
            bool last = false;
        if (last) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        {
            auto axis_word = s.read();
            w = axis_word.data;
            last = axis_word.last;
        }
        self->ntaps = (ap_uint<8>)(w.range(7, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        if (last) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        {
            auto axis_word = s.read();
            w = axis_word.data;
            last = axis_word.last;
        }
        self->nsamp_total = (ap_uint<32>)(w);
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        if (last) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        {
            auto axis_word = s.read();
            w = axis_word.data;
            last = axis_word.last;
        }
        self->nerr = (ap_uint<16>)(w.range(15, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->last_error = static_cast<FirError>(static_cast<unsigned int>(w.range(18, 16)));
        if (tl != streamutils::tlast_status::no_tlast) {
            return;
        }
        if (last) {
            tl = streamutils::tlast_status::tlast_at_end;
        }
    }

    static void read_axi4_stream_impl(word_bw_tag<64>, FirStatusMsg* self, hls::stream<streamutils::axi4s_word<64>> &s, streamutils::tlast_status &tl) {
            ap_uint<64> w = 0;
            tl = streamutils::tlast_status::no_tlast;
            bool last = false;
        if (last) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        {
            auto axis_word = s.read();
            w = axis_word.data;
            last = axis_word.last;
        }
        self->ntaps = (ap_uint<8>)(w.range(7, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->nsamp_total = (ap_uint<32>)(w.range(39, 8));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->nerr = (ap_uint<16>)(w.range(55, 40));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->last_error = static_cast<FirError>(static_cast<unsigned int>(w.range(58, 56)));
        if (tl != streamutils::tlast_status::no_tlast) {
            return;
        }
        if (last) {
            tl = streamutils::tlast_status::tlast_at_end;
        }
    }

    template<int word_bw>
    void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s, streamutils::tlast_status &tl) {
        read_axi4_stream_impl(word_bw_tag<word_bw>{}, this, s, tl);
    }

    template<int word_bw>
    void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s) {
        streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;
        read_axi4_stream<word_bw>(s, tl);
    }

#ifdef WAVEFLOW_ENABLE_FIR_STATUS_MSG_TB_H_MEMBERS
    void dump_json(std::ostream& os, int indent = 2, int level = 0) const;
    void load_json(const std::string& json_text, size_t& pos);
    void load_json(std::istream& is);
    void dump_json_file(const char* file_path, int indent = 2) const;
    void load_json_file(const char* file_path);
#endif
};

#endif // INCLUDE_FIR_STATUS_MSG_H