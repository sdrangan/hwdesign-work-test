// maxpool_tb.cpp -- the hand-written C++ testbench.
//
// Usage: maxpool_tb <data_dir> <stage> [scenario]
//
// <data_dir> is the scenario directory (data/w32).  For every scenario named in
// <data_dir>/scenarios.txt (or just [scenario]), in order, it runs the kernel once -- one
// ap_start, on fresh streams -- and records what came out:
//
//   1. poison the status registers, so a kernel that does not clear them (rule 5) fails;
//   2. play the stimulus <data_dir>/<scenario>/in into fresh streams and call the kernel;
//   3. record the response into <data_dir>/<scenario>/<stage>/, and the status in status.json;
//   4. discard whatever a halted kernel left unread: the host's reset (rule 7).
//
// The same binary runs csim and the one co-simulation: in cosim every scenario -- each error
// activation and the fresh activation after it included -- is one RTL transaction, in order.
// <stage> is "csim" or "cosim", so both runs keep their own results.  The output
// directories must exist; the Python build step that runs this creates them.

#include "gen/maxpool.hpp"
#include "include/bundle_tb.h"
#include "include/int16_array_utils_tb.h"

#include <cstdio>
#include <fstream>
#include <string>

int main(int argc, char** argv) {
    if (argc < 3) {
        std::fprintf(stderr, "usage: maxpool_tb <data_dir> <stage> [scenario]\n");
        return 2;
    }
    const std::string root = argv[1];
    const std::string stage = argv[2];
    const std::string only = (argc > 3) ? argv[3] : "";
    constexpr int W = 32;

    std::ifstream list(root + "/scenarios.txt");
    std::string name;
    int run = 0;
    while (list >> name) {
        if (!only.empty() && name != only) continue;
        const std::string dir = root + "/" + name;

        // 1. What a previous run would have left in the registers (maxpool.POISONED_STATUS).
        ap_uint<1> halted = 1;
        ap_uint<8> error = static_cast<unsigned int>(MaxpoolError::NO_TLAST_SAMP_IN);
        ap_uint<16> tx_id = 0xFFFF;

        // 2. One activation, on fresh streams.
        hls::stream<streamutils::axi4s_word<W>> s_in, m_out;
        wf::play_stream<W>(dir + "/in", s_in);
        maxpool(s_in, m_out, halted, error, tx_id);

        // 3. The response and the status.
        wf::record_stream<W>(m_out, dir + "/" + stage);
        std::ofstream st(dir + "/" + stage + "/status.json");
        st << "{\"halted\": " << (int)halted << ", \"error\": " << (int)error
           << ", \"tx_id\": " << (int)tx_id << "}\n";

        // 4. The host resets the stream path: commands queued behind an error are dropped.
        int dropped = 0;
        while (!s_in.empty()) { s_in.read(); ++dropped; }

        std::printf("scenario %-34s halted=%d error=%d tx_id=%d unread_words=%d\n",
                    name.c_str(), (int)halted, (int)error, (int)tx_id, dropped);
        ++run;
    }
    if (run == 0) {
        std::fprintf(stderr, "no scenario ran (data_dir=%s, only=%s)\n", root.c_str(), only.c_str());
        return 1;
    }
    return 0;
}
