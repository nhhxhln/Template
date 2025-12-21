#pragma once

#include <chrono>
#include <thread>
#include <csignal>
#include <fstream>
#include <filesystem>

extern "C" {
  #include <libavutil/opt.h>
  #include <libavutil/avutil.h>
  #include <libavcodec/avcodec.h>
  #include <libavformat/avformat.h>

  #include <libswscale/swscale.h>
}

#include <SDL2/SDL.h>

#include <libyuv.h>
#include <spdlog/spdlog.h>
#include <spdlog/fmt/ranges.h>
#include <libassert/assert.hpp>
#include <argparse/argparse.hpp>
#include <spdlog/fmt/bin_to_hex.h>
#include <magic_enum/magic_enum.hpp>
#include <spdlog/sinks/stdout_color_sinks.h>

