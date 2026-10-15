#ifndef INCLUDE_QAM16_LLR_ERROR_H
#define INCLUDE_QAM16_LLR_ERROR_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

enum class Qam16LlrError {
    NO_ERROR = 0,
    TLAST_EARLY_SAMP_IN = 1,
    NO_TLAST_SAMP_IN = 2,
    BAD_PARAM = 3,
    UNKNOWN_CMD = 4,
};

#endif // INCLUDE_QAM16_LLR_ERROR_H