#ifndef INCLUDE_FIR_RESP_FTR_H
#define INCLUDE_FIR_RESP_FTR_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

#include "fir_error.h"

struct FirRespFtr {
    ap_uint<16> nin;  // Input values consumed from the payload
    ap_uint<16> nout;  // Output samples written
    FirError error;  // Error code of this command

    static constexpr int bitwidth = 35;

    template<int word_bw>
    struct word_bw_tag {};

    template<int word_bw>
    static constexpr int nwords_value(word_bw_tag<word_bw>) {
            static_assert(word_bw < 0, "Unsupported word_bw for nwords");
            return 0;
    }

    static constexpr int nwords_value(word_bw_tag<32>) {
            return 2;
    }

    static constexpr int nwords_value(word_bw_tag<64>) {
            return 1;
    }

    template<int word_bw>
    static constexpr int nwords() {
        return nwords_value(word_bw_tag<word_bw>{});
    }

    static ap_uint<bitwidth> pack_to_uint(const FirRespFtr& data) {
        ap_uint<bitwidth> res = 0;
        res.range(15, 0) = data.nin;
        res.range(31, 16) = data.nout;
        res.range(34, 32) = (ap_uint<3>)(static_cast<unsigned int>(data.error));
        return res;
    }

    static FirRespFtr unpack_from_uint(const ap_uint<bitwidth>& packed) {
        FirRespFtr data;
        data.nin = (ap_uint<16>)(packed.range(15, 0));
        data.nout = (ap_uint<16>)(packed.range(31, 16));
        data.error = static_cast<FirError>(static_cast<unsigned int>(packed.range(34, 32)));
        return data;
    }

    template<int word_bw>
    static void write_array_impl(word_bw_tag<word_bw>, const FirRespFtr* self, ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_array");
        (void)self;
        (void)x;
    }

    static void write_array_impl(word_bw_tag<32>, const FirRespFtr* self, ap_uint<32> x[]) {
        x[0] = 0;
        x[0].range(15, 0) = self->nin;
        x[0].range(31, 16) = self->nout;
        x[1] = 0;
        x[1].range(2, 0) = (ap_uint<3>)(static_cast<unsigned int>(self->error));
    }

    static void write_array_impl(word_bw_tag<64>, const FirRespFtr* self, ap_uint<64> x[]) {
        x[0] = 0;
        x[0].range(15, 0) = self->nin;
        x[0].range(31, 16) = self->nout;
        x[0].range(34, 32) = (ap_uint<3>)(static_cast<unsigned int>(self->error));
    }

    template<int word_bw>
    void write_array(ap_uint<word_bw> x[]) const {
        write_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void write_stream_impl(word_bw_tag<word_bw>, const FirRespFtr* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_stream");
        (void)self;
        (void)s;
    }

    static void write_stream_impl(word_bw_tag<32>, const FirRespFtr* self, hls::stream<ap_uint<32>> &s) {
            ap_uint<32> w = 0;
        w.range(15, 0) = self->nin;
        w.range(31, 16) = self->nout;
        s.write(w);
        w = 0;
        w.range(2, 0) = (ap_uint<3>)(static_cast<unsigned int>(self->error));
        s.write(w);
    }

    static void write_stream_impl(word_bw_tag<64>, const FirRespFtr* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w.range(15, 0) = self->nin;
        w.range(31, 16) = self->nout;
        w.range(34, 32) = (ap_uint<3>)(static_cast<unsigned int>(self->error));
        s.write(w);
    }

    template<int word_bw>
    void write_stream(hls::stream<ap_uint<word_bw>> &s) const {
        write_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void write_axi4_stream_impl(word_bw_tag<word_bw>, const FirRespFtr* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_axi4_stream");
        (void)self;
        (void)s;
        (void)tlast;
    }

    static void write_axi4_stream_impl(word_bw_tag<32>, const FirRespFtr* self, hls::stream<streamutils::axi4s_word<32>> &s, bool tlast) {
            ap_uint<32> w = 0;
        w.range(15, 0) = self->nin;
        w.range(31, 16) = self->nout;
        streamutils::write_axi4_word<32>(s, w, false);
        w = 0;
        w.range(2, 0) = (ap_uint<3>)(static_cast<unsigned int>(self->error));
        streamutils::write_axi4_word<32>(s, w, tlast);
    }

    static void write_axi4_stream_impl(word_bw_tag<64>, const FirRespFtr* self, hls::stream<streamutils::axi4s_word<64>> &s, bool tlast) {
            ap_uint<64> w = 0;
        w.range(15, 0) = self->nin;
        w.range(31, 16) = self->nout;
        w.range(34, 32) = (ap_uint<3>)(static_cast<unsigned int>(self->error));
        streamutils::write_axi4_word<64>(s, w, tlast);
    }

    template<int word_bw>
    void write_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast = true) const {
        write_axi4_stream_impl(word_bw_tag<word_bw>{}, this, s, tlast);
    }

    template<int word_bw>
    static void read_array_impl(word_bw_tag<word_bw>, FirRespFtr* self, const ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_array");
        (void)self;
        (void)x;
    }

    static void read_array_impl(word_bw_tag<32>, FirRespFtr* self, const ap_uint<32> x[]) {
        self->nin = (ap_uint<16>)(x[0].range(15, 0));
        self->nout = (ap_uint<16>)(x[0].range(31, 16));
        self->error = static_cast<FirError>(static_cast<unsigned int>(x[1].range(2, 0)));
    }

    static void read_array_impl(word_bw_tag<64>, FirRespFtr* self, const ap_uint<64> x[]) {
        self->nin = (ap_uint<16>)(x[0].range(15, 0));
        self->nout = (ap_uint<16>)(x[0].range(31, 16));
        self->error = static_cast<FirError>(static_cast<unsigned int>(x[0].range(34, 32)));
    }

    template<int word_bw>
    void read_array(const ap_uint<word_bw> x[]) {
        read_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void read_stream_impl(word_bw_tag<word_bw>, FirRespFtr* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_stream");
        (void)self;
        (void)s;
    }

    static void read_stream_impl(word_bw_tag<32>, FirRespFtr* self, hls::stream<ap_uint<32>> &s) {
            ap_uint<32> w = 0;
        w = s.read();
        self->nin = (ap_uint<16>)(w.range(15, 0));
        self->nout = (ap_uint<16>)(w.range(31, 16));
        w = s.read();
        self->error = static_cast<FirError>(static_cast<unsigned int>(w.range(2, 0)));
    }

    static void read_stream_impl(word_bw_tag<64>, FirRespFtr* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w = s.read();
        self->nin = (ap_uint<16>)(w.range(15, 0));
        self->nout = (ap_uint<16>)(w.range(31, 16));
        self->error = static_cast<FirError>(static_cast<unsigned int>(w.range(34, 32)));
    }

    template<int word_bw>
    void read_stream(hls::stream<ap_uint<word_bw>> &s) {
        read_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void read_axi4_stream_impl(word_bw_tag<word_bw>, FirRespFtr* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, streamutils::tlast_status &tl) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_axi4_stream");
        (void)self;
        (void)s;
        (void)tl;
    }

    static void read_axi4_stream_impl(word_bw_tag<32>, FirRespFtr* self, hls::stream<streamutils::axi4s_word<32>> &s, streamutils::tlast_status &tl) {
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
        self->nin = (ap_uint<16>)(w.range(15, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->nout = (ap_uint<16>)(w.range(31, 16));
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
        self->error = static_cast<FirError>(static_cast<unsigned int>(w.range(2, 0)));
        if (tl != streamutils::tlast_status::no_tlast) {
            return;
        }
        if (last) {
            tl = streamutils::tlast_status::tlast_at_end;
        }
    }

    static void read_axi4_stream_impl(word_bw_tag<64>, FirRespFtr* self, hls::stream<streamutils::axi4s_word<64>> &s, streamutils::tlast_status &tl) {
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
        self->nin = (ap_uint<16>)(w.range(15, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->nout = (ap_uint<16>)(w.range(31, 16));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->error = static_cast<FirError>(static_cast<unsigned int>(w.range(34, 32)));
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

#ifdef WAVEFLOW_ENABLE_FIR_RESP_FTR_TB_H_MEMBERS
    void dump_json(std::ostream& os, int indent = 2, int level = 0) const;
    void load_json(const std::string& json_text, size_t& pos);
    void load_json(std::istream& is);
    void dump_json_file(const char* file_path, int indent = 2) const;
    void load_json_file(const char* file_path);
#endif
};

#endif // INCLUDE_FIR_RESP_FTR_H