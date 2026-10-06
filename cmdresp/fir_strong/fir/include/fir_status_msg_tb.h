#ifndef INCLUDE_FIR_STATUS_MSG_TB_H
#define INCLUDE_FIR_STATUS_MSG_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#include "fir_error_tb.h"

#define WAVEFLOW_ENABLE_FIR_STATUS_MSG_TB_H_MEMBERS
#include "fir_status_msg.h"
#undef WAVEFLOW_ENABLE_FIR_STATUS_MSG_TB_H_MEMBERS

inline void FirStatusMsg::dump_json(std::ostream& os, int indent, int level) const {
    const int step = (indent < 0) ? 0 : indent;
    os << "{";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"ntaps\": ";
    os << static_cast<unsigned long long>(this->ntaps);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"nsamp_total\": ";
    os << static_cast<unsigned long long>(this->nsamp_total);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"nerr\": ";
    os << static_cast<unsigned long long>(this->nerr);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"last_error\": ";
    os << static_cast<int>(this->last_error);
    os << "\n";
    for (int i = 0; i < (level) * step; ++i) { os << ' '; }
    os << "}";
}

inline void FirStatusMsg::load_json(const std::string& json_text, size_t& pos) {
    streamutils::json_expect_char(json_text, pos, '{');
    bool seen_root_ntaps = false;
    bool seen_root_nsamp_total = false;
    bool seen_root_nerr = false;
    bool seen_root_last_error = false;
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
    if (key == "ntaps") {
        seen_root_ntaps = true;
        this->ntaps = static_cast<ap_uint<8>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "nsamp_total") {
        seen_root_nsamp_total = true;
        this->nsamp_total = static_cast<ap_uint<32>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "nerr") {
        seen_root_nerr = true;
        this->nerr = static_cast<ap_uint<16>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "last_error") {
        seen_root_last_error = true;
        this->last_error = static_cast<FirError>(static_cast<long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else {
        throw std::runtime_error("Malformed JSON: unexpected key for schema.");
    }
    }
    if (!seen_root_ntaps) {
    throw std::runtime_error("Malformed JSON: missing required key 'ntaps'.");
    }
    if (!seen_root_nsamp_total) {
    throw std::runtime_error("Malformed JSON: missing required key 'nsamp_total'.");
    }
    if (!seen_root_nerr) {
    throw std::runtime_error("Malformed JSON: missing required key 'nerr'.");
    }
    if (!seen_root_last_error) {
    throw std::runtime_error("Malformed JSON: missing required key 'last_error'.");
    }
}

inline void FirStatusMsg::load_json(std::istream& is) {
    std::string json_text((std::istreambuf_iterator<char>(is)), std::istreambuf_iterator<char>());
    size_t pos = 0;
    streamutils::json_skip_ws(json_text, pos);
    this->load_json(json_text, pos);
    streamutils::json_skip_ws(json_text, pos);
    if (pos != json_text.size()) {
        throw std::runtime_error("Trailing characters after JSON object.");
    }
}

inline void FirStatusMsg::dump_json_file(const char* file_path, int indent) const {
    std::ofstream ofs(file_path);
    if (!ofs) {
        throw std::runtime_error("Failed to open output JSON file.");
    }
    this->dump_json(ofs, indent);
}

inline void FirStatusMsg::load_json_file(const char* file_path) {
    std::ifstream ifs(file_path);
    if (!ifs) {
        throw std::runtime_error("Failed to open input JSON file.");
    }
    this->load_json(ifs);
}

#endif // INCLUDE_FIR_STATUS_MSG_TB_H