#ifndef INCLUDE_FIR_RESP_FTR_TB_H
#define INCLUDE_FIR_RESP_FTR_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#include "fir_error_tb.h"

#define WAVEFLOW_ENABLE_FIR_RESP_FTR_TB_H_MEMBERS
#include "fir_resp_ftr.h"
#undef WAVEFLOW_ENABLE_FIR_RESP_FTR_TB_H_MEMBERS

inline void FirRespFtr::dump_json(std::ostream& os, int indent, int level) const {
    const int step = (indent < 0) ? 0 : indent;
    os << "{";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"nin\": ";
    os << static_cast<unsigned long long>(this->nin);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"nout\": ";
    os << static_cast<unsigned long long>(this->nout);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"error\": ";
    os << static_cast<int>(this->error);
    os << "\n";
    for (int i = 0; i < (level) * step; ++i) { os << ' '; }
    os << "}";
}

inline void FirRespFtr::load_json(const std::string& json_text, size_t& pos) {
    streamutils::json_expect_char(json_text, pos, '{');
    bool seen_root_nin = false;
    bool seen_root_nout = false;
    bool seen_root_error = false;
    bool first = true;
    while (true) {
    streamutils::json_skip_ws(json_text, pos);
    if (pos < json_text.size() && json_text[pos] == '}') {
        ++pos;
        break;
    }
    if (!first) {
        streamutils::json_expect_char(json_text, pos, ',');
    }
    first = false;
    std::string key = streamutils::json_parse_string(json_text, pos);
    streamutils::json_expect_char(json_text, pos, ':');
    if (key == "nin") {
        seen_root_nin = true;
        this->nin = static_cast<ap_uint<16>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "nout") {
        seen_root_nout = true;
        this->nout = static_cast<ap_uint<16>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "error") {
        seen_root_error = true;
        this->error = static_cast<FirError>(static_cast<long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else {
        throw std::runtime_error("Malformed JSON: unexpected key for schema.");
    }
    }
    if (!seen_root_nin) {
    throw std::runtime_error("Malformed JSON: missing required key 'nin'.");
    }
    if (!seen_root_nout) {
    throw std::runtime_error("Malformed JSON: missing required key 'nout'.");
    }
    if (!seen_root_error) {
    throw std::runtime_error("Malformed JSON: missing required key 'error'.");
    }
}

inline void FirRespFtr::load_json(std::istream& is) {
    std::string json_text((std::istreambuf_iterator<char>(is)), std::istreambuf_iterator<char>());
    size_t pos = 0;
    streamutils::json_skip_ws(json_text, pos);
    this->load_json(json_text, pos);
    streamutils::json_skip_ws(json_text, pos);
    if (pos != json_text.size()) {
        throw std::runtime_error("Trailing characters after JSON object.");
    }
}

inline void FirRespFtr::dump_json_file(const char* file_path, int indent) const {
    std::ofstream ofs(file_path);
    if (!ofs) {
        throw std::runtime_error("Failed to open output JSON file.");
    }
    this->dump_json(ofs, indent);
}

inline void FirRespFtr::load_json_file(const char* file_path) {
    std::ifstream ifs(file_path);
    if (!ifs) {
        throw std::runtime_error("Failed to open input JSON file.");
    }
    this->load_json(ifs);
}

#endif // INCLUDE_FIR_RESP_FTR_TB_H