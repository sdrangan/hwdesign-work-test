#ifndef INCLUDE_MAXPOOL_ERROR_H
#define INCLUDE_MAXPOOL_ERROR_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

enum class MaxpoolError {
    NO_ERROR = 0,
    TLAST_EARLY_SAMP_IN = 1,
    NO_TLAST_SAMP_IN = 2,
    BAD_PARAM = 3,
    BAD_CMD = 4,
    TLAST_EARLY_HDR_IN = 5,
    NO_TLAST_HDR_IN = 6,
};

#endif // INCLUDE_MAXPOOL_ERROR_H