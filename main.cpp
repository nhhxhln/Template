#include "pch.hpp"

using namespace std::chrono_literals;

static std::string root_logger = "root";
static std::shared_ptr<spdlog::logger> logger = nullptr;
static spdlog::level::level_enum log_level = spdlog::level::info;

static argparse::ArgumentParser parser("Sample", "1.0.0");
static void arg_parse(int argc, char *argv[]);

int main(int argc, char *argv[]) {
  arg_parse(argc, argv);
}

static void assert_failure_handler(const libassert::assertion_info &info) {
  libassert::enable_virtual_terminal_processing_if_needed(); // for terminal colors on windows
  std::cerr << info.to_string(libassert::terminal_width(STDERR_FILENO),
    isatty(STDERR_FILENO) ? libassert::get_color_scheme() : libassert::color_scheme::blank) << std::endl;
  std::ignore = fflush(stderr);
  switch(info.type) {
    case libassert::assert_type::assumption: break;
    case libassert::assert_type::panic:
    case libassert::assert_type::assertion:
    case libassert::assert_type::unreachable:
    case libassert::assert_type::debug_assertion:
      std::exit(SIGABRT); raise(SIGABRT); asm volatile("hlt"); break;
    default: LIBASSERT_PRIMITIVE_PANIC("Unknown assertion type in assertion failure handler");
  }
}

static void init_logger() {
  auto consle = spdlog::stdout_color_mt(root_logger);
  consle->set_pattern("%^%L (%H:%M:%S.%e) [%n]:%$ %v");
  logger = spdlog::get(root_logger);
  ASSERT(logger);
  logger->set_level(log_level);
}

static void arg_parse(int argc, char *argv[]) {
  libassert::set_failure_handler(assert_failure_handler);

  parser.add_group("logger config");
  parser.add_argument("-q").help("disable info print").flag();
  parser.add_argument("-g").help("debug output enable").flag();
  parser.add_argument("-gg").help("verbose output enable").flag();

  parser.add_group("input/output config");
  parser.add_argument("input").help("input file path").default_value("");
  parser.add_argument("-o", "--output").help("output file path").default_value("");

  try {
    parser.parse_args(argc, argv);
  } catch (const std::exception &e) {
    PANIC(e.what(), parser);
  }

  /*!< Config spdlog level */
  if (parser.is_used("-q")) log_level = spdlog::level::warn;
  if (parser.is_used("-g")) log_level = spdlog::level::debug;
  if (parser.is_used("-gg")) log_level = spdlog::level::trace;
  init_logger();
}
