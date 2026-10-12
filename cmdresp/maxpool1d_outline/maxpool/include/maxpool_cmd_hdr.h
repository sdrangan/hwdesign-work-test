#ifndef INCLUDE_MAXPOOL_CMD_HDR_H
#define INCLUDE_MAXPOOL_CMD_HDR_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

struct MaxpoolCmdHdr {
    ap_uint<8> cmd_type;  // MaxpoolCmdType code: COMPUTE or END
    ap_uint<16> n;  // Signal length in samples (0 for END)
    ap_uint<16> k;  // Window size in samples (0 for END)
    ap_uint<16> tx_id;  // Command ID: echoed, or reported on error

    static constexpr int bitwidth = 56;

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

    template<int word_bw>
    static constexpr int nwords() {
        return nwords_value(word_bw_tag<word_bw>{});
    }

    static ap_uint<bitwidth> pack_to_uint(const MaxpoolCmdHdr& data) {
        ap_uint<bitwidth> res = 0;
        res.range(7, 0) = data.cmd_type;
        res.range(23, 8) = data.n;
        res.range(39, 24) = data.k;
        res.range(55, 40) = data.tx_id;
        return res;
    }

    static MaxpoolCmdHdr unpack_from_uint(const ap_uint<bitwidth>& packed) {
        MaxpoolCmdHdr data;
        data.cmd_type = (ap_uint<8>)(packed.range(7, 0));
        data.n = (ap_uint<16>)(packed.range(23, 8));
        data.k = (ap_uint<16>)(packed.range(39, 24));
        data.tx_id = (ap_uint<16>)(packed.range(55, 40));
        return data;
    }

    template<int word_bw>
    static void write_array_impl(word_bw_tag<word_bw>, const MaxpoolCmdHdr* self, ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_array");
        (void)self;
        (void)x;
    }

    static void write_array_impl(word_bw_tag<32>, const MaxpoolCmdHdr* self, ap_uint<32> x[]) {
        x[0] = 0;
        x[0].range(7, 0) = self->cmd_type;
        x[0].range(23, 8) = self->n;
        x[1] = 0;
        x[1].range(15, 0) = self->k;
        x[1].range(31, 16) = self->tx_id;
    }

    template<int word_bw>
    void write_array(ap_uint<word_bw> x[]) const {
        write_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void write_stream_impl(word_bw_tag<word_bw>, const MaxpoolCmdHdr* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_stream");
        (void)self;
        (void)s;
    }

    static void write_stream_impl(word_bw_tag<32>, const MaxpoolCmdHdr* self, hls::stream<ap_uint<32>> &s) {
            ap_uint<32> w = 0;
        w.range(7, 0) = self->cmd_type;
        w.range(23, 8) = self->n;
        s.write(w);
        w = 0;
        w.range(15, 0) = self->k;
        w.range(31, 16) = self->tx_id;
        s.write(w);
        w = 0;
    }

    template<int word_bw>
    void write_stream(hls::stream<ap_uint<word_bw>> &s) const {
        write_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void write_axi4_stream_impl(word_bw_tag<word_bw>, const MaxpoolCmdHdr* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_axi4_stream");
        (void)self;
        (void)s;
        (void)tlast;
    }

    static void write_axi4_stream_impl(word_bw_tag<32>, const MaxpoolCmdHdr* self, hls::stream<streamutils::axi4s_word<32>> &s, bool tlast) {
            ap_uint<32> w = 0;
        w.range(7, 0) = self->cmd_type;
        w.range(23, 8) = self->n;
        streamutils::write_axi4_word<32>(s, w, false);
        w = 0;
        w.range(15, 0) = self->k;
        w.range(31, 16) = self->tx_id;
        streamutils::write_axi4_word<32>(s, w, tlast);
        w = 0;
    }

    template<int word_bw>
    void write_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast = true) const {
        write_axi4_stream_impl(word_bw_tag<word_bw>{}, this, s, tlast);
    }

    template<int word_bw>
    static void read_array_impl(word_bw_tag<word_bw>, MaxpoolCmdHdr* self, const ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_array");
        (void)self;
        (void)x;
    }

    static void read_array_impl(word_bw_tag<32>, MaxpoolCmdHdr* self, const ap_uint<32> x[]) {
        self->cmd_type = (ap_uint<8>)(x[0].range(7, 0));
        self->n = (ap_uint<16>)(x[0].range(23, 8));
        self->k = (ap_uint<16>)(x[1].range(15, 0));
        self->tx_id = (ap_uint<16>)(x[1].range(31, 16));
    }

    template<int word_bw>
    void read_array(const ap_uint<word_bw> x[]) {
        read_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void read_stream_impl(word_bw_tag<word_bw>, MaxpoolCmdHdr* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_stream");
        (void)self;
        (void)s;
    }

    static void read_stream_impl(word_bw_tag<32>, MaxpoolCmdHdr* self, hls::stream<ap_uint<32>> &s) {
            ap_uint<32> w = 0;
        w = s.read();
        self->cmd_type = (ap_uint<8>)(w.range(7, 0));
        self->n = (ap_uint<16>)(w.range(23, 8));
        w = s.read();
        self->k = (ap_uint<16>)(w.range(15, 0));
        self->tx_id = (ap_uint<16>)(w.range(31, 16));
    }

    template<int word_bw>
    void read_stream(hls::stream<ap_uint<word_bw>> &s) {
        read_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void read_axi4_stream_impl(word_bw_tag<word_bw>, MaxpoolCmdHdr* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, streamutils::tlast_status &tl) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_axi4_stream");
        (void)self;
        (void)s;
        (void)tl;
    }

    static void read_axi4_stream_impl(word_bw_tag<32>, MaxpoolCmdHdr* self, hls::stream<streamutils::axi4s_word<32>> &s, streamutils::tlast_status &tl) {
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
        self->cmd_type = (ap_uint<8>)(w.range(7, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->n = (ap_uint<16>)(w.range(23, 8));
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
        self->k = (ap_uint<16>)(w.range(15, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->tx_id = (ap_uint<16>)(w.range(31, 16));
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

#ifdef WAVEFLOW_ENABLE_MAXPOOL_CMD_HDR_TB_H_MEMBERS
    void dump_json(std::ostream& os, int indent = 2, int level = 0) const;
    void load_json(const std::string& json_text, size_t& pos);
    void load_json(std::istream& is);
    void dump_json_file(const char* file_path, int indent = 2) const;
    void load_json_file(const char* file_path);
#endif
};

#endif // INCLUDE_MAXPOOL_CMD_HDR_H