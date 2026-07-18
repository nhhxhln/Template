#!/usr/bin/env python3
"""Statistical analysis + plots for the random-yuv420p x265 experiment.

Reads experiment/results/*.csv and writes:
  results/plots/*.png            figures for the report
  results/summary_*.csv          aggregated statistics tables
"""
import json
import os

import numpy as np
import pandas as pd
from scipy import stats as sps
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(ROOT, "results")
PLOTS = os.path.join(RESULTS, "plots")
os.makedirs(PLOTS, exist_ok=True)

plt.rcParams.update({"figure.dpi": 110, "axes.grid": True, "grid.alpha": 0.3})


def load(name):
    p = os.path.join(RESULTS, name)
    if not os.path.exists(p):
        return None
    df = pd.read_csv(p)
    return df


def ci95(x):
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 2:
        return 0.0
    return sps.t.ppf(0.975, n - 1) * x.std(ddof=1) / np.sqrt(n)


def agg_ratio(df, keys):
    rows = []
    for key, g in df.groupby(keys, dropna=False):
        r = g["ratio_hevc"].astype(float)
        d = dict(zip(keys, key if isinstance(key, tuple) else (key,)))
        d.update({
            "n": len(r),
            "ratio_mean": r.mean(), "ratio_std": r.std(ddof=1) if len(r) > 1 else 0.0,
            "ratio_ci95": ci95(r),
            "ratio_q05": r.quantile(0.05), "ratio_q50": r.quantile(0.5),
            "ratio_q95": r.quantile(0.95),
            "p_smaller": (r < 1.0).mean(),
            "kbps_mean": g["kbps"].astype(float).mean(),
            "kbps_std": g["kbps"].astype(float).std(ddof=1) if len(g) > 1 else 0.0,
            "avg_qp_mean": pd.to_numeric(g["avg_qp"], errors="coerce").mean(),
            "psnr_all_mean": pd.to_numeric(g["psnr_all"], errors="coerce").mean(),
            "psnr_Y_mean": pd.to_numeric(g["psnr_Y"], errors="coerce").mean(),
            "psnr_U_mean": pd.to_numeric(g["psnr_U"], errors="coerce").mean(),
            "psnr_V_mean": pd.to_numeric(g["psnr_V"], errors="coerce").mean(),
            "encode_wall_mean": g["encode_wall_s"].astype(float).mean(),
        })
        if len(r) >= 8 and r.std(ddof=1) > 0:
            d["shapiro_p"] = sps.shapiro(r)[1]
        else:
            d["shapiro_p"] = np.nan
        rows.append(d)
    return pd.DataFrame(rows)


def critical_qp_per_sample(g):
    """Interpolated QP where ratio_hevc crosses 1.0 for one sample's QP sweep."""
    gg = g[g["rc_mode"] == "cqp"].copy()
    gg["qp"] = gg["qp"].astype(float)
    gg = gg.sort_values("qp")
    q = gg["qp"].to_numpy()
    r = gg["ratio_hevc"].to_numpy(dtype=float)
    above = r > 1.0
    if not above.any():
        return np.nan  # never above 1 -> critical below min tested QP
    if above.all():
        return np.inf
    # last index where ratio > 1 followed by <= 1
    for i in range(len(q) - 1):
        if r[i] > 1.0 >= r[i + 1]:
            # linear interpolation
            return q[i] + (r[i] - 1.0) / (r[i] - r[i + 1]) * (q[i + 1] - q[i])
    return np.nan


# ---------------------------------------------------------------------------
def analyze_core():
    df = load("core_sweep.csv")
    if df is None:
        print("no core_sweep.csv")
        return
    core = df[df["experiment"] == "core"].copy()
    core["config"] = (core["width"].astype(str) + "x" + core["height"].astype(str)
                      + " f" + core["frames"].astype(str) + " " + core["structure"])

    # ---- summary table
    cqp = core[core["rc_mode"] == "cqp"].copy()
    cqp["qp"] = cqp["qp"].astype(int)
    summ = agg_ratio(cqp, ["config", "qp"])
    ll = agg_ratio(core[core["rc_mode"] == "lossless"], ["config"])
    ll["qp"] = "lossless"
    summary = pd.concat([summ, ll])
    summary.to_csv(os.path.join(RESULTS, "summary_core.csv"), index=False)

    # ---- fig: ratio vs QP per config
    fig, ax = plt.subplots(figsize=(9, 6))
    for cfg, g in summ.groupby("config"):
        g = g.sort_values("qp")
        ax.errorbar(g["qp"], g["ratio_mean"], yerr=g["ratio_ci95"], marker="o",
                    ms=3, capsize=2, label=cfg)
    ax.axhline(1.0, color="k", ls="--", lw=1)
    ax.set_xlabel("QP (requested)"); ax.set_ylabel("compressed / raw size ratio")
    ax.set_title("Compression ratio vs QP, random yuv420p (mean ± 95% CI)")
    ax.legend(fontsize=7)
    fig.tight_layout(); fig.savefig(os.path.join(PLOTS, "ratio_vs_qp.png")); plt.close(fig)

    # ---- fig: bitrate vs QP (log scale)
    fig, ax = plt.subplots(figsize=(9, 6))
    for cfg, g in summ.groupby("config"):
        g = g.sort_values("qp")
        ax.errorbar(g["qp"], g["kbps_mean"], yerr=g["kbps_std"], marker="o", ms=3,
                    capsize=2, label=cfg)
    ax.set_yscale("log")
    ax.set_xlabel("QP (requested)"); ax.set_ylabel("bitrate kb/s (log)")
    ax.set_title("Bitrate vs QP, random yuv420p")
    ax.legend(fontsize=7)
    fig.tight_layout(); fig.savefig(os.path.join(PLOTS, "bitrate_vs_qp.png")); plt.close(fig)

    # ---- fig: frame structure comparison at 256x256 f10
    fig, ax = plt.subplots(figsize=(8, 5.5))
    sub = cqp[(cqp["width"] == 256) & (cqp["height"] == 256) & (cqp["frames"] == 10)]
    for st, g in agg_ratio(sub, ["structure", "qp"]).groupby("structure"):
        g = g.sort_values("qp")
        ax.errorbar(g["qp"], g["ratio_mean"], yerr=g["ratio_ci95"], marker="o", ms=3,
                    capsize=2, label=st)
    ax.axhline(1.0, color="k", ls="--", lw=1)
    ax.set_xlabel("QP"); ax.set_ylabel("size ratio")
    ax.set_title("Frame structure comparison (256x256, 10 frames)")
    ax.legend()
    fig.tight_layout(); fig.savefig(os.path.join(PLOTS, "structure_compare.png")); plt.close(fig)

    # ---- fig: P(output < raw) vs QP
    fig, ax = plt.subplots(figsize=(8, 5.5))
    for cfg, g in summ.groupby("config"):
        g = g.sort_values("qp")
        ax.plot(g["qp"], g["p_smaller"], marker="o", ms=3, label=cfg)
    ax.set_xlabel("QP"); ax.set_ylabel("P(compressed < raw)")
    ax.set_title("Probability that output is smaller than raw input")
    ax.legend(fontsize=7)
    fig.tight_layout(); fig.savefig(os.path.join(PLOTS, "p_smaller_vs_qp.png")); plt.close(fig)

    # ---- critical QP per sample
    crit_rows = []
    for (cfg, s), g in core.groupby(["config", "sample"]):
        c = critical_qp_per_sample(g)
        crit_rows.append({"config": cfg, "sample": s, "critical_qp": c})
    crit = pd.DataFrame(crit_rows)
    crit.to_csv(os.path.join(RESULTS, "critical_qp_samples.csv"), index=False)
    cs = crit.groupby("config")["critical_qp"].agg(
        n="count", mean="mean",
        std=lambda x: x.std(ddof=1),
        min="min", max="max")
    cs["ci95"] = crit.groupby("config")["critical_qp"].apply(ci95)
    cs.to_csv(os.path.join(RESULTS, "summary_critical_qp.csv"))

    fig, ax = plt.subplots(figsize=(9, 5.5))
    configs = sorted(crit["config"].unique())
    data = [crit[crit["config"] == c]["critical_qp"].dropna() for c in configs]
    ax.boxplot(data, tick_labels=configs, showmeans=True)
    ax.set_ylabel("critical QP (ratio = 1.0)")
    ax.set_title("Per-sample 1:1 critical QP distribution")
    plt.setp(ax.get_xticklabels(), rotation=20, ha="right", fontsize=7)
    fig.tight_layout(); fig.savefig(os.path.join(PLOTS, "critical_qp_dist.png")); plt.close(fig)

    # ---- output size distribution at selected QPs (256x256 f10 IPB)
    sub = cqp[(cqp["config"] == "256x256 f10 IPB")]
    sel = [12, 16, 18, 20, 24]
    fig, axes = plt.subplots(1, len(sel), figsize=(15, 3.2), sharey=False)
    for ax, qp in zip(axes, sel):
        r = sub[sub["qp"] == qp]["ratio_hevc"].astype(float)
        if len(r) == 0:
            continue
        ax.hist(r, bins=12, color="steelblue", edgecolor="k")
        ax.axvline(1.0, color="r", ls="--")
        ax.set_title(f"QP {qp}\nμ={r.mean():.4f} σ={r.std(ddof=1):.4f}", fontsize=8)
        ax.tick_params(labelsize=7)
    fig.suptitle("Output/raw ratio distribution, 256x256 f10 IPB (30 samples)")
    fig.tight_layout(); fig.savefig(os.path.join(PLOTS, "size_distribution.png")); plt.close(fig)

    # ---- per-plane PSNR vs QP
    fig, ax = plt.subplots(figsize=(8, 5.5))
    g = agg_ratio(sub, ["qp"]).sort_values("qp")
    ax.plot(g["qp"], g["psnr_Y_mean"], marker="o", label="Y")
    ax.plot(g["qp"], g["psnr_U_mean"], marker="s", label="U")
    ax.plot(g["qp"], g["psnr_V_mean"], marker="^", label="V")
    ax.set_xlabel("QP"); ax.set_ylabel("PSNR (dB)")
    ax.set_title("Per-plane PSNR vs QP (256x256 f10 IPB)")
    ax.legend()
    fig.tight_layout(); fig.savefig(os.path.join(PLOTS, "psnr_per_plane.png")); plt.close(fig)

    # ---- lossless consistency
    ll_rows = core[core["rc_mode"] == "lossless"]
    consistency = {
        "total_lossless_encodes": int(len(ll_rows)),
        "byte_identical": int(ll_rows["identical"].sum()),
        "all_identical": bool(ll_rows["identical"].all()),
        "ratio_mean": float(ll_rows["ratio_hevc"].mean()),
        "ratio_min": float(ll_rows["ratio_hevc"].min()),
        "ratio_max": float(ll_rows["ratio_hevc"].max()),
    }
    with open(os.path.join(RESULTS, "lossless_consistency.json"), "w") as f:
        json.dump(consistency, f, indent=1)
    print("lossless consistency:", consistency)

    # ---- CRF sanity
    crf = df[df["experiment"] == "crf"]
    if len(crf):
        agg_ratio(crf, ["qp"]).to_csv(os.path.join(RESULTS, "summary_crf.csv"), index=False)


def analyze_tools():
    df = load("tool_sensitivity.csv")
    if df is None:
        print("no tool_sensitivity.csv")
        return
    df["rc_key"] = df.apply(
        lambda r: "lossless" if r["rc_mode"] == "lossless" else f"qp{int(r['qp'])}", axis=1)
    rows = []
    for (variant, rc), g in df.groupby(["variant", "rc_key"]):
        rows.append({
            "variant": variant, "rc": rc, "n": len(g),
            "ratio_mean": g["ratio_hevc"].mean(), "ratio_ci95": ci95(g["ratio_hevc"]),
            "psnr_mean": pd.to_numeric(g["psnr_all"], errors="coerce").mean(),
            "wall_mean": g["encode_wall_s"].mean(),
            "identical_all": bool(g["identical"].all()),
            "frames_I": g["frames_I"].mean(), "frames_P": g["frames_P"].mean(),
            "frames_B": g["frames_B"].mean(),
        })
    t = pd.DataFrame(rows)
    base = t[t["variant"] == "baseline"].set_index("rc")
    t["ratio_delta_pct"] = t.apply(
        lambda r: 100 * (r["ratio_mean"] / base.loc[r["rc"], "ratio_mean"] - 1), axis=1)
    t["wall_delta_pct"] = t.apply(
        lambda r: 100 * (r["wall_mean"] / base.loc[r["rc"], "wall_mean"] - 1), axis=1)
    t["psnr_delta_db"] = t.apply(
        lambda r: r["psnr_mean"] - base.loc[r["rc"], "psnr_mean"]
        if pd.notna(r["psnr_mean"]) else np.nan, axis=1)
    t.sort_values(["rc", "ratio_delta_pct"]).to_csv(
        os.path.join(RESULTS, "summary_tools.csv"), index=False)

    for rc in sorted(t["rc"].unique()):
        sub = t[(t["rc"] == rc) & (t["variant"] != "baseline")].sort_values("ratio_delta_pct")
        fig, ax = plt.subplots(figsize=(9, 0.28 * len(sub) + 1.5))
        colors = ["tab:green" if v < 0 else "tab:red" for v in sub["ratio_delta_pct"]]
        ax.barh(sub["variant"], sub["ratio_delta_pct"], color=colors)
        ax.axvline(0, color="k", lw=1)
        ax.set_xlabel("Δ size vs baseline (%)")
        ax.set_title(f"Tool sensitivity ({rc}), random yuv420p 256x256 f10")
        ax.tick_params(axis="y", labelsize=7)
        fig.tight_layout()
        fig.savefig(os.path.join(PLOTS, f"tools_{rc}.png")); plt.close(fig)


def analyze_gray():
    g4 = load("core_sweep.csv")
    gr = load("gray_sweep.csv")
    if gr is None or g4 is None:
        print("missing gray/core csv")
        return
    gray_cqp = gr[gr["rc_mode"] == "cqp"].copy()
    gray_cqp["qp"] = gray_cqp["qp"].astype(int)
    yuv_cqp = g4[(g4["experiment"] == "core") & (g4["rc_mode"] == "cqp") &
                 (g4["width"] == 256) & (g4["height"] == 256) &
                 (g4["frames"] == 10) & (g4["structure"] == "IPB")].copy()
    yuv_cqp["qp"] = yuv_cqp["qp"].astype(int)

    a = agg_ratio(gray_cqp, ["qp"]).sort_values("qp")
    b = agg_ratio(yuv_cqp, ["qp"]).sort_values("qp")
    a["fmt"] = "gray (i400)"; b["fmt"] = "yuv420p"
    pd.concat([a, b]).to_csv(os.path.join(RESULTS, "summary_gray_vs_420.csv"), index=False)

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    axes[0].errorbar(a["qp"], a["ratio_mean"], yerr=a["ratio_ci95"], marker="o", label="gray (i400)")
    axes[0].errorbar(b["qp"], b["ratio_mean"], yerr=b["ratio_ci95"], marker="s", label="yuv420p")
    axes[0].axhline(1.0, color="k", ls="--", lw=1)
    axes[0].set_xlabel("QP"); axes[0].set_ylabel("size ratio"); axes[0].legend()
    axes[0].set_title("gray vs yuv420p: ratio vs QP")
    axes[1].plot(a["qp"], a["psnr_Y_mean"], marker="o", label="gray Y")
    axes[1].plot(b["qp"], b["psnr_Y_mean"], marker="s", label="420 Y")
    axes[1].plot(b["qp"], b["psnr_U_mean"], marker="^", label="420 U")
    axes[1].plot(b["qp"], b["psnr_V_mean"], marker="v", label="420 V")
    axes[1].set_xlabel("QP"); axes[1].set_ylabel("PSNR (dB)"); axes[1].legend()
    axes[1].set_title("Per-plane PSNR: gray vs 4:2:0")
    fig.tight_layout(); fig.savefig(os.path.join(PLOTS, "gray_vs_420.png")); plt.close(fig)

    # gray critical QP
    crit = []
    for s, g in gr.groupby("sample"):
        crit.append(critical_qp_per_sample(g))
    crit = pd.Series(crit).dropna()
    with open(os.path.join(RESULTS, "gray_critical_qp.json"), "w") as f:
        json.dump({"n": int(len(crit)), "mean": float(crit.mean()),
                   "std": float(crit.std(ddof=1)), "ci95": float(ci95(crit))}, f, indent=1)


def main():
    analyze_core()
    analyze_tools()
    analyze_gray()
    print("analysis complete; plots in", PLOTS)


if __name__ == "__main__":
    main()
