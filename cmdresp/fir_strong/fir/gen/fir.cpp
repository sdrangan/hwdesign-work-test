#include "fir.hpp"

void fir(
    hls::stream<streamutils::axi4s_word<32>>& s_in,
    hls::stream<streamutils::axi4s_word<32>>& m_out
) {
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE s_axilite port=return       bundle=control
    fir_impl::body(s_in, m_out);
}

void fir_bw64(
    hls::stream<streamutils::axi4s_word<64>>& s_in,
    hls::stream<streamutils::axi4s_word<64>>& m_out
) {
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE s_axilite port=return       bundle=control
    fir_impl::body(s_in, m_out);
}

