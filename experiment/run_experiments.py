#!/usr/bin/env python3
"""Experiment driver: random yuv420p -> ffmpeg+libx265 -> statistics.

Runs all encoding experiments and writes CSV/JSON results into
experiment/results/.  Every encode records: config, raw/bitstream/container
sizes, actual frame-type counts and QPs from the x265 log, encode wall time,
lossless byte-exactness, and lossy error metrics (per plane) via yuvtool.

Usage: python3 run_experiments.py [--stage all|randomness|core|tools|gray|hm]
"""
import argparse
import csv
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
YUVTOOL = os.path.join(REPO, "build", "yuvtool")
RESULTS = os.path.join(ROOT, "results")
TMP = os.environ.get("EXP_TMP", "/tmp/exp_work")
HM_DECODER = os.environ.get("HM_DECODER", "/tmp/hm/bin/TAppDecoderStatic")

os.makedirs(RESULTS, exist_ok=True)
os.makedirs(TMP, exist_ok=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def gen_yuv(path, w, h, frames, source="mt19937", seed=0, i400=False):
    cmd = [YUVTOOL, "gen", "-W", str(w), "-H", str(h), "-n", str(frames),
           "-s", source, "--seed", str(seed), "-o", path]
    if i400:
        cmd.append("--i400")
    r = run(cmd)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def yuv_stats(path, w, h, frames, i400=False):
    cmd = [YUVTOOL, "stats", path, "-W", str(w), "-H", str(h), "-n", str(frames)]
    if i400:
        cmd.append("--i400")
    r = run(cmd)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def yuv_compare(ref, dist, w, h, frames, i400=False):
    cmd = [YUVTOOL, "compare", ref, dist, "-W", str(w), "-H", str(h), "-n", str(frames)]
    if i400:
        cmd.append("--i400")
    r = run(cmd)
    assert r.returncode in (0, 1, 2), r.stderr
    return json.loads(r.stdout)


X265_FRAME_RE = re.compile(
    r"frame (I|P|B):\s*(\d+), Avg QP:\s*([\d.]+)\s+kb/s:\s*([\d.]+)")
X265_SUMMARY_RE = re.compile(
    r"encoded (\d+) frames in ([\d.]+)s \(([\d.]+) fps\), ([\d.]+) kb/s, Avg QP:\s*([\d.]+)")
X265_TOOLS_RE = re.compile(r"x265 \[info\]: tools:(.*)")


def parse_x265_log(log):
    out = {"frames_I": 0, "frames_P": 0, "frames_B": 0,
           "avgqp_I": "", "avgqp_P": "", "avgqp_B": "",
           "encoded_frames": 0, "encode_fps": 0.0, "kbps": 0.0, "avg_qp": ""}
    for m in X265_FRAME_RE.finditer(log):
        t, n, qp, _ = m.groups()
        out[f"frames_{t}"] = int(n)
        out[f"avgqp_{t}"] = float(qp)
    m = X265_SUMMARY_RE.search(log)
    if m:
        out["encoded_frames"] = int(m.group(1))
        out["encode_fps"] = float(m.group(3))
        out["kbps"] = float(m.group(4))
        out["avg_qp"] = float(m.group(5))
    tools = " ".join(t.strip() for t in X265_TOOLS_RE.findall(log))
    out["tools"] = tools
    out["lossless_rc"] = "Rate Control                        : Lossless" in log
    return out


def encode(yuv, w, h, frames, x265_params, out_hevc, pix_fmt="yuv420p"):
    """Encode raw yuv to Annex-B HEVC.  Returns (parsed_log_dict, wall_time, full_log)."""
    cmd = ["ffmpeg", "-hide_banner", "-y",
           "-f", "rawvideo", "-pix_fmt", pix_fmt, "-s", f"{w}x{h}", "-r", "25",
           "-i", yuv, "-c:v", "libx265",
           "-x265-params", x265_params, "-f", "hevc", out_hevc]
    t0 = time.monotonic()
    r = run(cmd)
    wall = time.monotonic() - t0
    assert r.returncode == 0, f"encode failed: {r.stderr[-2000:]}"
    return parse_x265_log(r.stderr), wall, r.stderr


def decode(hevc, out_yuv, pix_fmt="yuv420p"):
    r = run(["ffmpeg", "-hide_banner", "-y", "-i", hevc,
             "-f", "rawvideo", "-pix_fmt", pix_fmt, out_yuv])
    assert r.returncode == 0, f"decode failed: {r.stderr[-2000:]}"


def remux_mp4(hevc, out_mp4):
    r = run(["ffmpeg", "-hide_banner", "-y", "-i", hevc, "-c", "copy", out_mp4])
    assert r.returncode == 0, r.stderr[-1000:]
    return os.path.getsize(out_mp4)


BASE_PARAMS = "frame-threads=1:pools=4:log-level=info"


def gop_params(structure, frames):
    """x265 parameter fragments for the three tested frame structures."""
    if structure == "all-I":
        return "keyint=1:min-keyint=1:scenecut=0"
    if structure == "IP":
        return f"keyint={max(frames, 250)}:min-keyint={max(frames, 25)}:bframes=0:scenecut=0"
    if structure == "IPB":
        return f"keyint={max(frames, 250)}:min-keyint={max(frames, 25)}:bframes=4:b-adapt=2:scenecut=0"
    raise ValueError(structure)


CSV_FIELDS = [
    "experiment", "sample", "seed", "source", "width", "height", "frames",
    "pix_fmt", "structure", "rc_mode", "qp", "variant", "x265_params",
    "raw_bytes", "hevc_bytes", "mp4_bytes", "ratio_hevc", "ratio_mp4",
    "kbps", "avg_qp", "frames_I", "frames_P", "frames_B",
    "avgqp_I", "avgqp_P", "avgqp_B", "lossless_rc",
    "encode_wall_s", "encode_fps",
    "identical", "mae_all", "mse_all", "psnr_all", "max_err_all",
    "mae_Y", "mse_Y", "psnr_Y", "max_err_Y",
    "mae_U", "mse_U", "psnr_U", "max_err_U",
    "mae_V", "mse_V", "psnr_V", "max_err_V",
    "input_sha256", "tools",
]


def one_encode(row_base, yuv, w, h, frames, params, pix_fmt="yuv420p",
               keep_stream=None, do_mp4=False, log_out=None):
    hevc = os.path.join(TMP, "out.hevc")
    dec = os.path.join(TMP, "dec.yuv")
    parsed, wall, full_log = encode(yuv, w, h, frames, params, hevc, pix_fmt)
    raw_bytes = os.path.getsize(yuv)
    hevc_bytes = os.path.getsize(hevc)
    mp4_bytes = ""
    if do_mp4:
        mp4_bytes = remux_mp4(hevc, os.path.join(TMP, "out.mp4"))
    decode(hevc, dec, pix_fmt)
    cmpres = yuv_compare(yuv, dec, w, h, frames, i400=(pix_fmt == "gray"))
    row = dict(row_base)
    row.update({
        "x265_params": params,
        "raw_bytes": raw_bytes, "hevc_bytes": hevc_bytes, "mp4_bytes": mp4_bytes,
        "ratio_hevc": hevc_bytes / raw_bytes,
        "ratio_mp4": (mp4_bytes / raw_bytes) if mp4_bytes else "",
        "kbps": parsed["kbps"], "avg_qp": parsed["avg_qp"],
        "frames_I": parsed["frames_I"], "frames_P": parsed["frames_P"],
        "frames_B": parsed["frames_B"],
        "avgqp_I": parsed["avgqp_I"], "avgqp_P": parsed["avgqp_P"],
        "avgqp_B": parsed["avgqp_B"], "lossless_rc": parsed["lossless_rc"],
        "encode_wall_s": round(wall, 4), "encode_fps": parsed["encode_fps"],
        "identical": cmpres["identical"],
        "tools": parsed["tools"],
    })
    for plane in ("all", "Y", "U", "V"):
        if plane in cmpres:
            row[f"mae_{plane}"] = cmpres[plane]["mae"]
            row[f"mse_{plane}"] = cmpres[plane]["mse"]
            row[f"psnr_{plane}"] = cmpres[plane]["psnr_db"]
            row[f"max_err_{plane}"] = cmpres[plane]["max_err"]
        else:
            row[f"mae_{plane}"] = row[f"mse_{plane}"] = row[f"psnr_{plane}"] = row[f"max_err_{plane}"] = ""
    if keep_stream:
        shutil.copy(hevc, keep_stream)
    if log_out:
        with open(log_out, "w") as f:
            f.write(full_log)
    return row


class CsvWriter:
    def __init__(self, name):
        self.path = os.path.join(RESULTS, name)
        exists = os.path.exists(self.path)
        self.f = open(self.path, "a", newline="")
        self.w = csv.DictWriter(self.f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if not exists:
            self.w.writeheader()

    def write(self, row):
        self.w.writerow(row)
        self.f.flush()


# ---------------------------------------------------------------------------
# Stage 1: randomness validation
# ---------------------------------------------------------------------------
def stage_randomness():
    out = {}
    w, h, n = 256, 256, 10
    for source, seeds in [("mt19937", [1, 2, 3]), ("urandom", [0]), ("random", [0])]:
        for seed in seeds:
            key = f"{source}_seed{seed}"
            yuv = os.path.join(TMP, "rand.yuv")
            geninfo = gen_yuv(yuv, w, h, n, source, seed)
            geninfo["sha256"] = sha256(yuv)
            st = yuv_stats(yuv, w, h, n)
            out[key] = {"gen": geninfo, "stats": st}
            print(f"[randomness] {key}: entropy={st['all']['entropy_bits']:.4f} "
                  f"chi2={st['all']['chi2']:.1f} adj_corr={st['all']['adj_corr']:.2e}")
    with open(os.path.join(RESULTS, "randomness.json"), "w") as f:
        json.dump(out, f, indent=1)


# ---------------------------------------------------------------------------
# Stage 2: core QP sweep
# ---------------------------------------------------------------------------
QP_LIST = [0, 4, 8, 10, 12, 14, 16, 18, 20, 24, 28, 32, 36, 40, 44, 48, 51]


def core_configs():
    # (w, h, frames, n_samples)
    return [
        (64, 64, 10, 20),
        (128, 128, 10, 20),
        (256, 256, 10, 30),
        (640, 360, 10, 10),
        (256, 256, 1, 20),
        (256, 256, 30, 10),
    ]


def stage_core():
    csvw = CsvWriter("core_sweep.csv")
    kept_dir = os.path.join(RESULTS, "streams")
    os.makedirs(kept_dir, exist_ok=True)
    logs_dir = os.path.join(RESULTS, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    for (w, h, frames, nsamp) in core_configs():
        structures = ["all-I", "IP", "IPB"] if (w, h, frames) == (256, 256, 10) else ["IPB"]
        if frames == 1:
            structures = ["all-I"]
        for structure in structures:
            for s in range(nsamp):
                seed = hash((w, h, frames, structure, s)) & 0xFFFFFFFF
                yuv = os.path.join(TMP, "in.yuv")
                gen_yuv(yuv, w, h, frames, "mt19937", seed)
                insha = sha256(yuv)
                base = {"experiment": "core", "sample": s, "seed": seed,
                        "source": "mt19937", "width": w, "height": h,
                        "frames": frames, "pix_fmt": "yuv420p",
                        "structure": structure, "variant": "",
                        "input_sha256": insha}
                gp = gop_params(structure, frames)
                # lossless
                keep = None
                log_out = None
                if s == 0:
                    keep = os.path.join(kept_dir, f"core_{w}x{h}_f{frames}_{structure}_lossless.hevc")
                    log_out = os.path.join(logs_dir, f"core_{w}x{h}_f{frames}_{structure}_lossless.log")
                row = one_encode({**base, "rc_mode": "lossless", "qp": ""},
                                 yuv, w, h, frames,
                                 f"{BASE_PARAMS}:{gp}:lossless=1",
                                 keep_stream=keep, do_mp4=True, log_out=log_out)
                csvw.write(row)
                # fixed QP
                for qp in QP_LIST:
                    keep = None
                    log_out = None
                    if s == 0 and qp in (0, 16, 32, 51):
                        keep = os.path.join(kept_dir, f"core_{w}x{h}_f{frames}_{structure}_qp{qp}.hevc")
                        log_out = os.path.join(logs_dir, f"core_{w}x{h}_f{frames}_{structure}_qp{qp}.log")
                    row = one_encode({**base, "rc_mode": "cqp", "qp": qp},
                                     yuv, w, h, frames,
                                     f"{BASE_PARAMS}:{gp}:qp={qp}",
                                     keep_stream=keep, do_mp4=(s == 0), log_out=log_out)
                    csvw.write(row)
                print(f"[core] {w}x{h} f{frames} {structure} sample {s} done")
    # CRF sanity points (one structure, one resolution)
    for s in range(10):
        seed = hash(("crf", s)) & 0xFFFFFFFF
        yuv = os.path.join(TMP, "in.yuv")
        gen_yuv(yuv, 256, 256, 10, "mt19937", seed)
        insha = sha256(yuv)
        for crf in (0, 10, 18, 28, 40, 51):
            row = one_encode({"experiment": "crf", "sample": s, "seed": seed,
                              "source": "mt19937", "width": 256, "height": 256,
                              "frames": 10, "pix_fmt": "yuv420p", "structure": "IPB",
                              "rc_mode": "crf", "qp": crf, "variant": "",
                              "input_sha256": insha},
                             yuv, 256, 256, 10,
                             f"{BASE_PARAMS}:{gop_params('IPB', 10)}:crf={crf}")
            csvw.write(row)
        print(f"[crf] sample {s} done")


# ---------------------------------------------------------------------------
# Stage 3: tool sensitivity
# ---------------------------------------------------------------------------
def tool_variants():
    """(variant_name, extra x265 params). One knob at a time vs the baseline."""
    return [
        ("baseline", ""),
        ("no-sao", "sao=0"),
        ("sao", "sao=1"),
        ("no-deblock", "deblock=0"),  # x265: deblock enabled by default
        ("deblock", "deblock=1"),
        ("tskip", "tskip=1"),
        ("tskip-fast", "tskip=1:tskip-fast=1"),
        ("amp", "amp=1"),
        ("no-rect", "rect=0"),
        ("rd1", "rd=1"),
        ("rd3", "rd=3"),
        ("rd5", "rd=5"),
        ("rdoq0", "rdoq-level=0"),
        ("rdoq2", "rdoq-level=2"),
        ("no-early-skip", "early-skip=0"),
        ("early-skip", "early-skip=1"),
        ("cu-lossless", "cu-lossless=1"),
        ("ref1", "ref=1"),
        ("ref5", "ref=5"),
        ("no-weightp", "weightp=0"),
        ("weightb", "weightb=1"),
        ("scenecut-default", "scenecut=40"),
        ("aq0", "aq-mode=0"),
        ("aq1", "aq-mode=1"),
        ("aq2", "aq-mode=2"),
        ("aq3", "aq-mode=3"),
        ("psy-rd0", "psy-rd=0"),
        ("psy-rd2", "psy-rd=2.0"),
        ("psy-rdoq2.5", "psy-rdoq=2.5:rdoq-level=2"),
        ("ctu32", "ctu=32"),
        ("ctu16", "ctu=16"),
        ("no-signhide", "signhide=0"),
        ("no-tmvp", "tmvp=0"),
        ("no-strong-intra-smoothing", "strong-intra-smoothing=0"),
        ("constrained-intra", "constrained-intra=1"),
        ("b-intra", "b-intra=1"),
        ("no-b-pyramid", "b-pyramid=0"),
        ("no-cutree", "cutree=0"),
        ("no-open-gop", "open-gop=0"),
    ]


def stage_tools():
    csvw = CsvWriter("tool_sensitivity.csv")
    logs_dir = os.path.join(RESULTS, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    w, h, frames = 256, 256, 10
    gp = gop_params("IPB", frames)
    n_samples = 5
    rc_modes = [("lossless", None), ("cqp", 16), ("cqp", 32)]
    for s in range(n_samples):
        seed = hash(("tools", s)) & 0xFFFFFFFF
        yuv = os.path.join(TMP, "in.yuv")
        gen_yuv(yuv, w, h, frames, "mt19937", seed)
        insha = sha256(yuv)
        for (variant, extra) in tool_variants():
            for rc, qp in rc_modes:
                rcp = "lossless=1" if rc == "lossless" else f"qp={qp}"
                params = f"{BASE_PARAMS}:{gp}:{rcp}"
                if extra:
                    params += f":{extra}"
                log_out = None
                if s == 0:
                    log_out = os.path.join(
                        logs_dir, f"tools_{variant}_{rc}{qp if qp is not None else ''}.log")
                row = one_encode(
                    {"experiment": "tools", "sample": s, "seed": seed,
                     "source": "mt19937", "width": w, "height": h, "frames": frames,
                     "pix_fmt": "yuv420p", "structure": "IPB", "rc_mode": rc,
                     "qp": qp if qp is not None else "", "variant": variant,
                     "input_sha256": insha},
                    yuv, w, h, frames, params, log_out=log_out)
                csvw.write(row)
        print(f"[tools] sample {s} done")


# ---------------------------------------------------------------------------
# Stage 4: gray (i400) vs 4:2:0
# ---------------------------------------------------------------------------
def stage_gray():
    csvw = CsvWriter("gray_sweep.csv")
    w, h, frames = 256, 256, 10
    gp = gop_params("IPB", frames)
    for s in range(15):
        seed = hash(("gray", s)) & 0xFFFFFFFF
        yuv = os.path.join(TMP, "in.yuv")
        gen_yuv(yuv, w, h, frames, "mt19937", seed, i400=True)
        insha = sha256(yuv)
        base = {"experiment": "gray", "sample": s, "seed": seed,
                "source": "mt19937", "width": w, "height": h, "frames": frames,
                "pix_fmt": "gray", "structure": "IPB", "variant": "",
                "input_sha256": insha}
        row = one_encode({**base, "rc_mode": "lossless", "qp": ""},
                         yuv, w, h, frames, f"{BASE_PARAMS}:{gp}:lossless=1",
                         pix_fmt="gray")
        csvw.write(row)
        for qp in QP_LIST:
            row = one_encode({**base, "rc_mode": "cqp", "qp": qp},
                             yuv, w, h, frames, f"{BASE_PARAMS}:{gp}:qp={qp}",
                             pix_fmt="gray")
            csvw.write(row)
        print(f"[gray] sample {s} done")


# ---------------------------------------------------------------------------
# Stage 5: HM cross-check of saved streams
# ---------------------------------------------------------------------------
def stage_hm():
    if not os.path.exists(HM_DECODER):
        print(f"HM decoder not found at {HM_DECODER}; skipping")
        return
    kept_dir = os.path.join(RESULTS, "streams")
    out = {}
    for name in sorted(os.listdir(kept_dir)):
        if not name.endswith(".hevc"):
            continue
        stream = os.path.join(kept_dir, name)
        rec = os.path.join(TMP, "hm_rec.yuv")
        if os.path.exists(rec):
            os.remove(rec)
        r = run([HM_DECODER, "-b", stream, "-o", rec])
        ffdec = os.path.join(TMP, "ff_rec.yuv")
        run(["ffmpeg", "-hide_banner", "-y", "-i", stream,
             "-f", "rawvideo", "-pix_fmt", "yuv420p", ffdec])
        entry = {"hm_exit": r.returncode,
                 "hm_tail": r.stdout.strip().splitlines()[-3:] if r.stdout else [],
                 "hm_stderr_tail": r.stderr.strip().splitlines()[-3:] if r.stderr else []}
        if r.returncode == 0 and os.path.exists(rec):
            same = (os.path.getsize(rec) == os.path.getsize(ffdec) and
                    open(rec, "rb").read() == open(ffdec, "rb").read())
            entry["hm_matches_ffmpeg_decode"] = same
            entry["hm_rec_bytes"] = os.path.getsize(rec)
        out[name] = entry
        print(f"[hm] {name}: exit={r.returncode} "
              f"match_ffmpeg={entry.get('hm_matches_ffmpeg_decode')}")
    with open(os.path.join(RESULTS, "hm_check.json"), "w") as f:
        json.dump(out, f, indent=1)


def write_sysinfo():
    info = {
        "platform": platform.platform(),
        "python": sys.version,
        "cpu_count": os.cpu_count(),
        "ffmpeg": run(["ffmpeg", "-version"]).stdout.splitlines()[:3],
        "x265_cli": run(["x265", "--version"]).stderr.splitlines()[:2],
        "date": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }
    lscpu = run(["lscpu"])
    info["lscpu"] = [l for l in lscpu.stdout.splitlines()
                     if any(k in l for k in ("Model name", "CPU(s):", "MHz", "Architecture"))]
    with open(os.path.join(RESULTS, "sysinfo.json"), "w") as f:
        json.dump(info, f, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all",
                    choices=["all", "randomness", "core", "tools", "gray", "hm"])
    args = ap.parse_args()
    write_sysinfo()
    stages = {
        "randomness": stage_randomness,
        "core": stage_core,
        "tools": stage_tools,
        "gray": stage_gray,
        "hm": stage_hm,
    }
    if args.stage == "all":
        for fn in stages.values():
            fn()
    else:
        stages[args.stage]()


if __name__ == "__main__":
    main()
