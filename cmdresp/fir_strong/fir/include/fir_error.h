#ifndef INCLUDE_FIR_ERROR_H
#define INCLUDE_FIR_ERROR_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

enum class FirError {
    NO_ERROR = 0,
    BAD_OPCODE = 1,
    BAD_NTAPS = 2,
    NO_TAPS = 3,
    BAD_DECIM = 4,
    TLAST_EARLY = 5,
    NO_TLAST = 6,
};

#endif // INCLUDE_FIR_ERROR_H