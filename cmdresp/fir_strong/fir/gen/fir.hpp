#pragma once

#include <ap_int.h>
#include <ap_fixed.h>

#include "include/streamutils_hls.h"

void fir(
    hls::stream<streamutils::axi4s_word<32>>& s_in,
    hls::stream<streamutils::axi4s_word<32>>& m_out
);

void fir_bw64(
    hls::stream<streamutils::axi4s_word<64>>& s_in,
    hls::stream<streamutils::axi4s_word<64>>& m_out
);

namespace fir_impl {
    template <int in_bw, int out_bw>
    void body(hls::stream<streamutils::axi4s_word<in_bw>>& s_in, hls::stream<streamutils::axi4s_word<out_bw>>& m_out);
}

#include "../fir_body_impl.tpp"
