#ifndef INCLUDE_QAM16_LLR_CMD_TYPE_TB_H
#define INCLUDE_QAM16_LLR_CMD_TYPE_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#define WAVEFLOW_ENABLE_QAM16_LLR_CMD_TYPE_TB_H_MEMBERS
#include "qam16_llr_cmd_type.h"
#undef WAVEFLOW_ENABLE_QAM16_LLR_CMD_TYPE_TB_H_MEMBERS

inline const char* enum_to_string(Qam16LlrCmdType value) {
    switch (value) {
    case Qam16LlrCmdType::DEMAP:
        return "DEMAP";
    case Qam16LlrCmdType::END:
        return "END";
    default:
        return "UNKNOWN";
    }
}

#endif // INCLUDE_QAM16_LLR_CMD_TYPE_TB_H