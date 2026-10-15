#ifndef INCLUDE_QAM16_LLR_ERROR_TB_H
#define INCLUDE_QAM16_LLR_ERROR_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#define WAVEFLOW_ENABLE_QAM16_LLR_ERROR_TB_H_MEMBERS
#include "qam16_llr_error.h"
#undef WAVEFLOW_ENABLE_QAM16_LLR_ERROR_TB_H_MEMBERS

inline const char* enum_to_string(Qam16LlrError value) {
    switch (value) {
    case Qam16LlrError::NO_ERROR:
        return "NO_ERROR";
    case Qam16LlrError::TLAST_EARLY_SAMP_IN:
        return "TLAST_EARLY_SAMP_IN";
    case Qam16LlrError::NO_TLAST_SAMP_IN:
        return "NO_TLAST_SAMP_IN";
    case Qam16LlrError::BAD_PARAM:
        return "BAD_PARAM";
    case Qam16LlrError::UNKNOWN_CMD:
        return "UNKNOWN_CMD";
    default:
        return "UNKNOWN";
    }
}

#endif // INCLUDE_QAM16_LLR_ERROR_TB_H