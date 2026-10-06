#ifndef INCLUDE_FIR_ERROR_TB_H
#define INCLUDE_FIR_ERROR_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#define WAVEFLOW_ENABLE_FIR_ERROR_TB_H_MEMBERS
#include "fir_error.h"
#undef WAVEFLOW_ENABLE_FIR_ERROR_TB_H_MEMBERS

inline const char* enum_to_string(FirError value) {
    switch (value) {
    case FirError::NO_ERROR:
        return "NO_ERROR";
    case FirError::BAD_OPCODE:
        return "BAD_OPCODE";
    case FirError::BAD_NTAPS:
        return "BAD_NTAPS";
    case FirError::NO_TAPS:
        return "NO_TAPS";
    case FirError::BAD_DECIM:
        return "BAD_DECIM";
    case FirError::TLAST_EARLY:
        return "TLAST_EARLY";
    case FirError::NO_TLAST:
        return "NO_TLAST";
    default:
        return "UNKNOWN";
    }
}

#endif // INCLUDE_FIR_ERROR_TB_H