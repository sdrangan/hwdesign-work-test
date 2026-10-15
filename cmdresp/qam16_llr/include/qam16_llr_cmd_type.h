#ifndef INCLUDE_QAM16_LLR_CMD_TYPE_H
#define INCLUDE_QAM16_LLR_CMD_TYPE_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

enum class Qam16LlrCmdType {
    DEMAP = 0,
    END = 1,
};

#endif // INCLUDE_QAM16_LLR_CMD_TYPE_H