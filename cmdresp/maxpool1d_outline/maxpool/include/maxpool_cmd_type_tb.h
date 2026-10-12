#ifndef INCLUDE_MAXPOOL_CMD_TYPE_TB_H
#define INCLUDE_MAXPOOL_CMD_TYPE_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#define WAVEFLOW_ENABLE_MAXPOOL_CMD_TYPE_TB_H_MEMBERS
#include "maxpool_cmd_type.h"
#undef WAVEFLOW_ENABLE_MAXPOOL_CMD_TYPE_TB_H_MEMBERS

inline const char* enum_to_string(MaxpoolCmdType value) {
    switch (value) {
    case MaxpoolCmdType::COMPUTE:
        return "COMPUTE";
    case MaxpoolCmdType::END:
        return "END";
    default:
        return "UNKNOWN";
    }
}

#endif // INCLUDE_MAXPOOL_CMD_TYPE_TB_H