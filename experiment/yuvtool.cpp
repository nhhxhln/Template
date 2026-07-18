// yuvtool: random YUV generation, randomness statistics, and byte/metric comparison
// Part of the random-yuv420p x265 encoding experiment.
//
// Subcommands:
//   gen     - generate random planar YUV (i420 or i400) from urandom / mt19937 / random
//   stats   - randomness statistics of a raw file (entropy, byte freq, correlations, repeats)
//   compare - byte-exact + MAE/MSE/PSNR/max-error comparison of two yuv420p/yuv400 files
//
// All numeric results are printed as a single JSON object on stdout for easy scripting.
#include "pch.hpp"

#include <array>
#include <cmath>
#include <cstdint>
#include <random>
#include <string>
#include <vector>
#include <unordered_set>

using u8 = std::uint8_t;
using u64 = std::uint64_t;

static std::shared_ptr<spdlog::logger> logger = spdlog::stderr_color_mt("yuvtool");

struct PlaneGeom {
  std::size_t y_size, u_size, v_size;
};

static PlaneGeom geom(std::size_t w, std::size_t h, bool i400) {
  PlaneGeom g{};
  g.y_size = w * h;
  g.u_size = i400 ? 0 : (w / 2) * (h / 2);
  g.v_size = g.u_size;
  return g;
}

// ---------------------------------------------------------------------------
// generation
// ---------------------------------------------------------------------------
static void fill_urandom(std::vector<u8> &buf, const std::string &dev) {
  std::ifstream f(dev, std::ios::binary);
  ASSERT(f.good(), "cannot open random device", dev);
  f.read(reinterpret_cast<char *>(buf.data()), static_cast<std::streamsize>(buf.size()));
  ASSERT(static_cast<std::size_t>(f.gcount()) == buf.size(), "short read from device", dev);
}

static void fill_mt19937(std::vector<u8> &buf, u64 seed) {
  std::mt19937_64 rng(seed);
  std::size_t i = 0;
  for (; i + 8 <= buf.size(); i += 8) {
    u64 v = rng();
    std::memcpy(buf.data() + i, &v, 8);
  }
  if (i < buf.size()) {
    u64 v = rng();
    std::memcpy(buf.data() + i, &v, buf.size() - i);
  }
}

// FNV-1a 64-bit checksum (self-contained, reproducible)
static u64 fnv1a(const std::vector<u8> &buf) {
  u64 h = 1469598103934665603ULL;
  for (u8 b : buf) { h ^= b; h *= 1099511628211ULL; }
  return h;
}

// ---------------------------------------------------------------------------
// statistics
// ---------------------------------------------------------------------------
struct RandStats {
  double entropy_bits;       // Shannon entropy per byte
  double chi2;               // chi-square vs uniform(256)
  double mean;
  double adj_corr;           // lag-1 Pearson correlation within the buffer
  double dup16_ratio;        // fraction of duplicated 16-byte blocks
  u64 min_count, max_count;
};

static RandStats rand_stats(const u8 *p, std::size_t n) {
  std::array<u64, 256> hist{};
  for (std::size_t i = 0; i < n; i++) hist[p[i]]++;
  RandStats s{};
  double exp_c = static_cast<double>(n) / 256.0;
  s.min_count = UINT64_MAX; s.max_count = 0;
  double sum = 0;
  for (int v = 0; v < 256; v++) {
    double c = static_cast<double>(hist[v]);
    if (c > 0) {
      double pr = c / static_cast<double>(n);
      s.entropy_bits -= pr * std::log2(pr);
    }
    s.chi2 += (c - exp_c) * (c - exp_c) / exp_c;
    sum += c * v;
    s.min_count = std::min<u64>(s.min_count, hist[v]);
    s.max_count = std::max<u64>(s.max_count, hist[v]);
  }
  s.mean = sum / static_cast<double>(n);

  // lag-1 correlation
  double sx = 0, sy = 0, sxx = 0, syy = 0, sxy = 0;
  std::size_t m = n - 1;
  for (std::size_t i = 0; i < m; i++) {
    double x = p[i], y = p[i + 1];
    sx += x; sy += y; sxx += x * x; syy += y * y; sxy += x * y;
  }
  double dm = static_cast<double>(m);
  double cov = sxy / dm - (sx / dm) * (sy / dm);
  double vx = sxx / dm - (sx / dm) * (sx / dm);
  double vy = syy / dm - (sy / dm) * (sy / dm);
  s.adj_corr = cov / std::sqrt(vx * vy);

  // duplicated 16-byte blocks
  std::unordered_set<u64> seen;
  u64 dups = 0, blocks = 0;
  for (std::size_t i = 0; i + 16 <= n; i += 16, blocks++) {
    u64 h = 1469598103934665603ULL;
    for (int k = 0; k < 16; k++) { h ^= p[i + k]; h *= 1099511628211ULL; }
    if (!seen.insert(h).second) dups++;
  }
  s.dup16_ratio = blocks ? static_cast<double>(dups) / static_cast<double>(blocks) : 0.0;
  return s;
}

static double pearson(const u8 *a, const u8 *b, std::size_t n) {
  double sx = 0, sy = 0, sxx = 0, syy = 0, sxy = 0;
  for (std::size_t i = 0; i < n; i++) {
    double x = a[i], y = b[i];
    sx += x; sy += y; sxx += x * x; syy += y * y; sxy += x * y;
  }
  double dn = static_cast<double>(n);
  double cov = sxy / dn - (sx / dn) * (sy / dn);
  double vx = sxx / dn - (sx / dn) * (sx / dn);
  double vy = syy / dn - (sy / dn) * (sy / dn);
  return cov / std::sqrt(vx * vy);
}

static void print_stats_json(const char *name, const RandStats &s, bool comma) {
  fmt::print("  \"{}\": {{\"entropy_bits\": {:.6f}, \"chi2\": {:.2f}, \"mean\": {:.4f}, "
             "\"adj_corr\": {:.6e}, \"dup16_ratio\": {:.6e}, \"min_count\": {}, \"max_count\": {}}}{}\n",
             name, s.entropy_bits, s.chi2, s.mean, s.adj_corr, s.dup16_ratio,
             s.min_count, s.max_count, comma ? "," : "");
}

static std::vector<u8> read_file(const std::string &path) {
  std::ifstream f(path, std::ios::binary | std::ios::ate);
  ASSERT(f.good(), "cannot open file", path);
  auto sz = static_cast<std::size_t>(f.tellg());
  f.seekg(0);
  std::vector<u8> buf(sz);
  f.read(reinterpret_cast<char *>(buf.data()), static_cast<std::streamsize>(sz));
  return buf;
}

// ---------------------------------------------------------------------------
// commands
// ---------------------------------------------------------------------------
static int cmd_gen(int w, int h, int frames, const std::string &source, u64 seed,
                   bool i400, const std::string &out) {
  auto g = geom(static_cast<std::size_t>(w), static_cast<std::size_t>(h), i400);
  std::size_t frame_size = g.y_size + g.u_size + g.v_size;
  std::vector<u8> buf(frame_size * static_cast<std::size_t>(frames));

  if (source == "urandom") fill_urandom(buf, "/dev/urandom");
  else if (source == "random") fill_urandom(buf, "/dev/random");
  else if (source == "mt19937") fill_mt19937(buf, seed);
  else { logger->error("unknown source: {}", source); return 1; }

  std::ofstream f(out, std::ios::binary);
  ASSERT(f.good(), "cannot open output", out);
  f.write(reinterpret_cast<const char *>(buf.data()), static_cast<std::streamsize>(buf.size()));
  f.close();

  fmt::print("{{\n  \"file\": \"{}\", \"source\": \"{}\", \"seed\": {}, \"width\": {}, \"height\": {}, "
             "\"frames\": {}, \"format\": \"{}\", \"bytes\": {}, \"fnv1a64\": \"{:016x}\"\n}}\n",
             out, source, seed, w, h, frames, i400 ? "yuv400p" : "yuv420p", buf.size(), fnv1a(buf));
  return 0;
}

static int cmd_stats(const std::string &path, int w, int h, int frames, bool i400) {
  auto buf = read_file(path);
  auto g = geom(static_cast<std::size_t>(w), static_cast<std::size_t>(h), i400);
  std::size_t frame_size = g.y_size + g.u_size + g.v_size;
  ASSERT(buf.size() == frame_size * static_cast<std::size_t>(frames), "file size mismatch",
         buf.size(), frame_size * frames);

  fmt::print("{{\n");
  auto all = rand_stats(buf.data(), buf.size());
  print_stats_json("all", all, true);

  // per-plane aggregated across frames
  std::vector<u8> ally, allu, allv;
  for (int f = 0; f < frames; f++) {
    const u8 *base = buf.data() + static_cast<std::size_t>(f) * frame_size;
    ally.insert(ally.end(), base, base + g.y_size);
    if (!i400) {
      allu.insert(allu.end(), base + g.y_size, base + g.y_size + g.u_size);
      allv.insert(allv.end(), base + g.y_size + g.u_size, base + frame_size);
    }
  }
  print_stats_json("Y", rand_stats(ally.data(), ally.size()), true);
  if (!i400) {
    print_stats_json("U", rand_stats(allu.data(), allu.size()), true);
    print_stats_json("V", rand_stats(allv.data(), allv.size()), true);
    fmt::print("  \"uv_corr\": {:.6e},\n", pearson(allu.data(), allv.data(), allu.size()));
    fmt::print("  \"yu_corr\": {:.6e},\n", pearson(ally.data(), allu.data(), allu.size()));
  }

  // frame-to-frame identical check
  u64 identical_frames = 0;
  for (int f = 1; f < frames; f++)
    if (std::memcmp(buf.data() + static_cast<std::size_t>(f - 1) * frame_size,
                    buf.data() + static_cast<std::size_t>(f) * frame_size, frame_size) == 0)
      identical_frames++;
  fmt::print("  \"identical_adjacent_frames\": {},\n  \"fnv1a64\": \"{:016x}\"\n}}\n",
             identical_frames, fnv1a(buf));
  return 0;
}

struct PlaneErr {
  u64 n = 0, diff = 0, max_err = 0, sae = 0;
  double sse = 0;
  std::array<u64, 256> err_hist{};
  void add(u8 a, u8 b) {
    n++;
    int e = std::abs(static_cast<int>(a) - static_cast<int>(b));
    if (e) diff++;
    max_err = std::max<u64>(max_err, static_cast<u64>(e));
    sae += static_cast<u64>(e);
    sse += static_cast<double>(e) * e;
    err_hist[static_cast<std::size_t>(e)]++;
  }
  void print(const char *name, bool comma) const {
    double mae = static_cast<double>(sae) / static_cast<double>(n);
    double mse = sse / static_cast<double>(n);
    double psnr = mse > 0 ? 10.0 * std::log10(255.0 * 255.0 / mse) : std::numeric_limits<double>::infinity();
    fmt::print("  \"{}\": {{\"n\": {}, \"diff_bytes\": {}, \"identical\": {}, \"mae\": {:.6f}, "
               "\"mse\": {:.6f}, \"psnr_db\": {}, \"max_err\": {}, \"err_hist_nonzero\": {{",
               name, n, diff, diff == 0 ? "true" : "false", mae, mse,
               std::isinf(psnr) ? std::string("\"inf\"") : fmt::format("{:.4f}", psnr), max_err);
    bool first = true;
    for (std::size_t e = 0; e < 256; e++) {
      if (err_hist[e]) {
        fmt::print("{}\"{}\": {}", first ? "" : ", ", e, err_hist[e]);
        first = false;
      }
    }
    fmt::print("}}}}{}\n", comma ? "," : "");
  }
};

static int cmd_compare(const std::string &ref, const std::string &dist, int w, int h,
                       int frames, bool i400) {
  auto a = read_file(ref), b = read_file(dist);
  auto g = geom(static_cast<std::size_t>(w), static_cast<std::size_t>(h), i400);
  std::size_t frame_size = g.y_size + g.u_size + g.v_size;
  std::size_t expect = frame_size * static_cast<std::size_t>(frames);
  fmt::print("{{\n  \"ref_bytes\": {}, \"dist_bytes\": {}, \"expected_bytes\": {}, \"size_match\": {},\n",
             a.size(), b.size(), expect, (a.size() == expect && b.size() == expect) ? "true" : "false");
  if (a.size() != b.size()) {
    fmt::print("  \"identical\": false, \"reason\": \"size mismatch\"\n}}\n");
    return 2;
  }
  PlaneErr py, pu, pv, pall;
  for (int f = 0; f < frames; f++) {
    std::size_t base = static_cast<std::size_t>(f) * frame_size;
    for (std::size_t i = 0; i < g.y_size; i++) { py.add(a[base + i], b[base + i]); pall.add(a[base + i], b[base + i]); }
    for (std::size_t i = 0; i < g.u_size; i++) {
      std::size_t o = base + g.y_size + i;
      pu.add(a[o], b[o]); pall.add(a[o], b[o]);
    }
    for (std::size_t i = 0; i < g.v_size; i++) {
      std::size_t o = base + g.y_size + g.u_size + i;
      pv.add(a[o], b[o]); pall.add(a[o], b[o]);
    }
  }
  py.print("Y", true);
  if (!i400) { pu.print("U", true); pv.print("V", true); }
  pall.print("all", true);
  bool identical = std::memcmp(a.data(), b.data(), a.size()) == 0;
  fmt::print("  \"identical\": {}\n}}\n", identical ? "true" : "false");
  return identical ? 0 : 1;
}

int main(int argc, char *argv[]) {
  argparse::ArgumentParser prog("yuvtool", "1.0.0");

  argparse::ArgumentParser gen("gen");
  gen.add_description("generate random planar YUV");
  gen.add_argument("-W", "--width").scan<'i', int>().required();
  gen.add_argument("-H", "--height").scan<'i', int>().required();
  gen.add_argument("-n", "--frames").scan<'i', int>().default_value(1);
  gen.add_argument("-s", "--source").default_value(std::string("mt19937"))
     .help("urandom | random | mt19937");
  gen.add_argument("--seed").scan<'u', u64>().default_value(u64{0});
  gen.add_argument("--i400").flag().help("luma-only yuv400");
  gen.add_argument("-o", "--output").required();

  argparse::ArgumentParser stats("stats");
  stats.add_description("randomness statistics of a raw YUV file");
  stats.add_argument("file");
  stats.add_argument("-W", "--width").scan<'i', int>().required();
  stats.add_argument("-H", "--height").scan<'i', int>().required();
  stats.add_argument("-n", "--frames").scan<'i', int>().default_value(1);
  stats.add_argument("--i400").flag();

  argparse::ArgumentParser cmp("compare");
  cmp.add_description("byte-exact + error-metric comparison of two raw YUV files");
  cmp.add_argument("ref");
  cmp.add_argument("dist");
  cmp.add_argument("-W", "--width").scan<'i', int>().required();
  cmp.add_argument("-H", "--height").scan<'i', int>().required();
  cmp.add_argument("-n", "--frames").scan<'i', int>().default_value(1);
  cmp.add_argument("--i400").flag();

  prog.add_subparser(gen);
  prog.add_subparser(stats);
  prog.add_subparser(cmp);

  try {
    prog.parse_args(argc, argv);
  } catch (const std::exception &e) {
    logger->error("{}", e.what());
    std::cerr << prog;
    return 1;
  }

  if (prog.is_subcommand_used("gen"))
    return cmd_gen(gen.get<int>("--width"), gen.get<int>("--height"), gen.get<int>("--frames"),
                   gen.get<std::string>("--source"), gen.get<u64>("--seed"),
                   gen.get<bool>("--i400"), gen.get<std::string>("--output"));
  if (prog.is_subcommand_used("stats"))
    return cmd_stats(stats.get<std::string>("file"), stats.get<int>("--width"),
                     stats.get<int>("--height"), stats.get<int>("--frames"), stats.get<bool>("--i400"));
  if (prog.is_subcommand_used("compare"))
    return cmd_compare(cmp.get<std::string>("ref"), cmp.get<std::string>("dist"),
                       cmp.get<int>("--width"), cmp.get<int>("--height"),
                       cmp.get<int>("--frames"), cmp.get<bool>("--i400"));
  std::cerr << prog;
  return 1;
}
