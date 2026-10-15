// qam16_llr_tb.cpp -- the hand-written C++ testbench.
//
// Usage: qam16_llr_tb <data_dir> <stage> [scenario]        (built with -DPOLY_WORD_BW=32)
//
// <data_dir> is the scenario directory (data/w32).  For every scenario, in order,
// named in <data_dir>/scenarios.txt (or just [scenario]) it runs the kernel once -- one
// ap_start -- and records what came out:
//
//   1. poison the status registers, so a kernel that does not clear them (rule 5) fails;
//   2. play the stimulus <data_dir>/<scenario>/in into fresh streams and call the kernel;
//   3. record the response into <data_dir>/<scenario>/<stage>/, and the status in status.json;
//   4. discard whatever a halted kernel left unread: the host's reset (rule 7).
//
// <stage> is "csim" or "cosim", so both runs keep their own results.  The output
// directories must exist; the Python build step that runs this creates them.
//
// The stimulus is the same file the Python model reads (qam16_llr.py, qam16_llr_stream_model):
// one input, two implementations, and scenarios.py checks both against the expected
// outputs it computed from each scenario's intent.

#include "gen/qam16_llr.hpp"
#include "include/bundle_tb.h"

#include <cstdio>
#include <fstream>
#include <string>

#ifndef POLY_WORD_BW
#define POLY_WORD_BW 32
#endif
#if POLY_WORD_BW == 32
#define POLY_TOP qam16_llr
#else
#error "POLY_WORD_BW must be 32"
#endif

int main(int argc, char** argv) {
    if (argc < 3) {
        std::fprintf(stderr, "usage: qam16_llr_tb <data_dir> <stage> [scenario]\n");
        return 2;
    }
    const std::string root = argv[1];
    const std::string stage = argv[2];
    const std::string only = (argc > 3) ? argv[3] : "";
    constexpr int W = POLY_WORD_BW;

    std::ifstream list(root + "/scenarios.txt");
    std::string name;
    int run = 0;
    while (list >> name) {
        if (!only.empty() && name != only) continue;
        const std::string dir = root + "/" + name;

        // 1. What a previous run would have left in the registers (qam16_llr.POISONED_STATUS).
        ap_uint<1> halted = 1;
        ap_uint<8> error = static_cast<unsigned int>(Qam16LlrError::NO_TLAST_SAMP_IN);
        ap_uint<16> tx_id = 0xFFFF;

        // 2. One activation, on fresh streams.
        hls::stream<streamutils::axi4s_word<W>> s_in, m_out;
        wf::play_stream<W>(dir + "/in", s_in);
        POLY_TOP(s_in, m_out, halted, error, tx_id);

        // 3. The response and the status.
        wf::record_stream<W>(m_out, dir + "/" + stage);
        std::ofstream st(dir + "/" + stage + "/status.json");
        st << "{\"halted\": " << (int)halted << ", \"error\": " << (int)error
           << ", \"tx_id\": " << (int)tx_id << "}\n";

        // 4. The host resets the stream path: commands queued behind an error are dropped.
        int dropped = 0;
        while (!s_in.empty()) { s_in.read(); ++dropped; }

        std::printf("w%d scenario %-16s halted=%d error=%d tx_id=%d unread_words=%d\n", W,
                    name.c_str(), (int)halted, (int)error, (int)tx_id, dropped);
        ++run;
    }
    if (run == 0) {
        std::fprintf(stderr, "no scenario ran (data_dir=%s, only=%s)\n", root.c_str(), only.c_str());
        return 1;
    }
    return 0;
}
