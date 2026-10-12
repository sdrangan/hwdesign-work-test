#ifndef INCLUDE_MAXPOOL_ERROR_TB_H
#define INCLUDE_MAXPOOL_ERROR_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#define WAVEFLOW_ENABLE_MAXPOOL_ERROR_TB_H_MEMBERS
#include "maxpool_error.h"
#undef WAVEFLOW_ENABLE_MAXPOOL_ERROR_TB_H_MEMBERS

inline const char* enum_to_string(MaxpoolError value) {
    switch (value) {
    case MaxpoolError::NO_ERROR:
        return "NO_ERROR";
    case MaxpoolError::TLAST_EARLY_SAMP_IN:
        return "TLAST_EARLY_SAMP_IN";
    case MaxpoolError::NO_TLAST_SAMP_IN:
        return "NO_TLAST_SAMP_IN";
    case MaxpoolError::BAD_PARAM:
        return "BAD_PARAM";
    case MaxpoolError::TLAST_EARLY_HDR:
        return "TLAST_EARLY_HDR";
    case MaxpoolError::NO_TLAST_HDR:
        return "NO_TLAST_HDR";
    case MaxpoolError::UNKNOWN_CMD:
        return "UNKNOWN_CMD";
    default:
        return "UNKNOWN";
    }
}

#endif // INCLUDE_MAXPOOL_ERROR_TB_H