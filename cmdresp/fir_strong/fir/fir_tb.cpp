// fir_tb.cpp -- the hand-written C++ testbench.
//
// Usage: fir_tb <data_dir> <stage>          (built with -DWORD_BW=32 or -DWORD_BW=64)
//
// Runs every scenario named in <data_dir>/scenarios.txt, IN ORDER, as one session: the
// kernel keeps its taps, delay line and counters from one scenario to the next.  For each
// scenario it plays the stimulus <data_dir>/<scenario>/in into the input stream, calls the
// kernel once per command (<scenario>/ncmd.txt), and records the output stream into
// <data_dir>/<scenario>/<stage>/.  <stage> is "csim" or "cosim".  The output directories
// must exist; the Python build step that runs this creates them.
//
// The stimulus is the same file the Python model reads (fir.py, fir_stream_model), and
// scenarios.py checks both against the expected outputs it computed from the intents.

#include "gen/fir.hpp"
#include "include/bundle_tb.h"

#include <cstdio>
#include <fstream>
#include <string>

#ifndef WORD_BW
#define WORD_BW 32
#endif

typedef hls::stream<streamutils::axi4s_word<WORD_BW>> word_stream;

// One command through the kernel top for this width.
static void run_kernel(word_stream& s_in, word_stream& m_out) {
#if WORD_BW == 32
    fir(s_in, m_out);
#else
    fir_bw64(s_in, m_out);
#endif
}

int main(int argc, char** argv) {
    if (argc < 3) {
        std::fprintf(stderr, "usage: fir_tb <data_dir> <stage>\n");
        return 2;
    }
    const std::string root = argv[1];
    const std::string stage = argv[2];

    std::ifstream list(root + "/scenarios.txt");
    std::string name;
    int run = 0, bad = 0;
    while (list >> name) {
        const std::string dir = root + "/" + name;
        int ncmd = 0;
        std::ifstream(dir + "/ncmd.txt") >> ncmd;

        word_stream s_in("s_in"), m_out("m_out");
        const int nin = wf::play_stream<WORD_BW>(dir + "/in", s_in);
        for (int c = 0; c < ncmd; ++c) run_kernel(s_in, m_out);
        const int nout = wf::record_stream<WORD_BW>(m_out, dir + "/" + stage);
        int left = 0;
        while (!s_in.empty()) { s_in.read(); ++left; }
        if (left) ++bad;   // every command consumes exactly its own words
        std::printf("scenario %-16s commands=%-3d words in=%-5d out=%-5d unread=%d\n",
                    name.c_str(), ncmd, nin, nout, left);
        ++run;
    }
    if (run == 0) {
        std::fprintf(stderr, "no scenario ran (data_dir=%s)\n", root.c_str());
        return 1;
    }
    return bad ? 1 : 0;
}
