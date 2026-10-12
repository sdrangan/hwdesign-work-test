#ifndef INCLUDE_MAXPOOL_CMD_HDR_TB_H
#define INCLUDE_MAXPOOL_CMD_HDR_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#define WAVEFLOW_ENABLE_MAXPOOL_CMD_HDR_TB_H_MEMBERS
#include "maxpool_cmd_hdr.h"
#undef WAVEFLOW_ENABLE_MAXPOOL_CMD_HDR_TB_H_MEMBERS

inline void MaxpoolCmdHdr::dump_json(std::ostream& os, int indent, int level) const {
    const int step = (indent < 0) ? 0 : indent;
    os << "{";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"cmd_type\": ";
    os << static_cast<unsigned long long>(this->cmd_type);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"tx_id\": ";
    os << static_cast<unsigned long long>(this->tx_id);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"n0\": ";
    os << static_cast<unsigned long long>(this->n0);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"n1\": ";
    os << static_cast<unsigned long long>(this->n1);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"s0\": ";
    os << static_cast<unsigned long long>(this->s0);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"s1\": ";
    os << static_cast<unsigned long long>(this->s1);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"k0\": ";
    os << static_cast<unsigned long long>(this->k0);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"k1\": ";
    os << static_cast<unsigned long long>(this->k1);
    os << "\n";
    for (int i = 0; i < (level) * step; ++i) { os << ' '; }
    os << "}";
}

inline void MaxpoolCmdHdr::load_json(const std::string& json_text, size_t& pos) {
    streamutils::json_expect_char(json_text, pos, '{');
    bool seen_root_cmd_type = false;
    bool seen_root_tx_id = false;
    bool seen_root_n0 = false;
    bool seen_root_n1 = false;
    bool seen_root_s0 = false;
    bool seen_root_s1 = false;
    bool seen_root_k0 = false;
    bool seen_root_k1 = false;
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
    if (key == "cmd_type") {
        seen_root_cmd_type = true;
        this->cmd_type = static_cast<ap_uint<8>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "tx_id") {
        seen_root_tx_id = true;
        this->tx_id = static_cast<ap_uint<16>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "n0") {
        seen_root_n0 = true;
        this->n0 = static_cast<ap_uint<16>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "n1") {
        seen_root_n1 = true;
        this->n1 = static_cast<ap_uint<16>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "s0") {
        seen_root_s0 = true;
        this->s0 = static_cast<ap_uint<8>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "s1") {
        seen_root_s1 = true;
        this->s1 = static_cast<ap_uint<8>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "k0") {
        seen_root_k0 = true;
        this->k0 = static_cast<ap_uint<8>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "k1") {
        seen_root_k1 = true;
        this->k1 = static_cast<ap_uint<8>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else {
        throw std::runtime_error("Malformed JSON: unexpected key for schema.");
    }
    }
    if (!seen_root_cmd_type) {
    throw std::runtime_error("Malformed JSON: missing required key 'cmd_type'.");
    }
    if (!seen_root_tx_id) {
    throw std::runtime_error("Malformed JSON: missing required key 'tx_id'.");
    }
    if (!seen_root_n0) {
    throw std::runtime_error("Malformed JSON: missing required key 'n0'.");
    }
    if (!seen_root_n1) {
    throw std::runtime_error("Malformed JSON: missing required key 'n1'.");
    }
    if (!seen_root_s0) {
    throw std::runtime_error("Malformed JSON: missing required key 's0'.");
    }
    if (!seen_root_s1) {
    throw std::runtime_error("Malformed JSON: missing required key 's1'.");
    }
    if (!seen_root_k0) {
    throw std::runtime_error("Malformed JSON: missing required key 'k0'.");
    }
    if (!seen_root_k1) {
    throw std::runtime_error("Malformed JSON: missing required key 'k1'.");
    }
}

inline void MaxpoolCmdHdr::load_json(std::istream& is) {
    std::string json_text((std::istreambuf_iterator<char>(is)), std::istreambuf_iterator<char>());
    size_t pos = 0;
    streamutils::json_skip_ws(json_text, pos);
    this->load_json(json_text, pos);
    streamutils::json_skip_ws(json_text, pos);
    if (pos != json_text.size()) {
        throw std::runtime_error("Trailing characters after JSON object.");
    }
}

inline void MaxpoolCmdHdr::dump_json_file(const char* file_path, int indent) const {
    std::ofstream ofs(file_path);
    if (!ofs) {
        throw std::runtime_error("Failed to open output JSON file.");
    }
    this->dump_json(ofs, indent);
}

inline void MaxpoolCmdHdr::load_json_file(const char* file_path) {
    std::ifstream ifs(file_path);
    if (!ifs) {
        throw std::runtime_error("Failed to open input JSON file.");
    }
    this->load_json(ifs);
}

#endif // INCLUDE_MAXPOOL_CMD_HDR_TB_H