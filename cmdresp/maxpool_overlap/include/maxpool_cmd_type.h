#ifndef INCLUDE_MAXPOOL_CMD_TYPE_H
#define INCLUDE_MAXPOOL_CMD_TYPE_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

enum class MaxpoolCmdType {
    COMPUTE = 0,
    END = 1,
};

#endif // INCLUDE_MAXPOOL_CMD_TYPE_H