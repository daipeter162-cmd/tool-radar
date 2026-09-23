#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tool-radar 趋势图

从 data/history.csv 读历史，画各品类每日新增 star 的趋势。

同时出中英两个版本（README.md 是英文默认，引用英文图；README.zh-CN.md
引用中文图）—— 两个 README 共用一个中文图是不对的。

为什么用**小倍数图**（每格一个品类）而不是一张图 11 条彩线：
分类色最多只能安全地用 8 个，第 9 个开始颜色在色觉障碍下就分不开了。
与其硬凑颜色，不如分面 —— 每格只有一条线，不需要图例，也不会混淆。

数据不足 2 天时自动降级为「当前各品类规模」横向柱状图。
只有一个点连不成趋势，画折线没有意义。

用法：
    python chart.py                # 中英两张都出
    python chart.py --lang zh      # 只出中文
    python chart.py --lang en      # 只出英文

依赖 matplotlib（可选）：
    pip install matplotlib

没装 matplotlib 会直接提示并退出，不影响采集流程。

产出：
    reports/trend.zh.png   中文图
    reports/trend.en.png   英文图
    reports/trend.csv      图上的原始数值（表格版，无障碍要求）
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
HISTORY_PATH = ROOT / "data" / "history.csv"
CONFIG_PATH = ROOT / "config.json"
REPORT_DIR = ROOT / "reports"
OUT_CSV = REPORT_DIR / "trend.csv"

# 配色：单色方案，来自已验证的调色板（validate_palette.js 全项 PASS）
SERIES = "#2a78d6"       # 分类槽位 1，蓝
SURFACE = "#fcfcfb"      # 图表底色
INK = "#0b0b0b"          # 主文字
INK_MUTED = "#898781"    # 轴标签
GRID = "#e1e0d9"         # 网格发丝线（实线，不用虚线）
BASELINE = "#c3c2b7"     # 基线

STRINGS = {
    "zh": {
        "trend_title": "各品类每日新增 star（只统计两天都在榜的项目）",
        "scale_title": "各品类中位 star（{date}，仅 {n} 天数据，趋势图需积累 2 天以上）",
        "no_data": "数据不足",
    },
    "en": {
        "trend_title": "Daily new stars per category (projects present on both days only)",
        "scale_title": "Median stars per category ({date} - only {n} day of data; trend needs 2+)",
        "no_data": "no data",
    },
}

# 中文字体候选。英文版不需要，matplotlib 自带 DejaVu Sans 就够。
CJK_FONTS = [
    "Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Noto Sans CJK JP",
    "Source Han Sans CN", "WenQuanYi Zen Hei", "PingFang SC", "Arial Unicode MS",
]


def load_names():
    """读 config.json 里的英文品类名。没有 en 字段的退回中文名。"""
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open(encoding="utf-8") as f:
        config = json.load(f)
    return {cat: spec.get("en") or cat
            for cat, spec in config.get("categories", {}).items()}


def load_history():
    """返回 {date: {category: {repo: stars}}}，只取 GitHub 源。"""
    if not HISTORY_PATH.exists():
        sys.exit(f"找不到 {HISTORY_PATH}，先跑一次 collect.py")

    series = defaultdict(lambda: defaultdict(dict))
    with HISTORY_PATH.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("source") or "github") != "github":
                continue
            day, cat, name = row.get("date"), row.get("category"), row.get("name")
            if not (day and cat and name):
                continue
            try:
                stars = int(row.get("stars") or 0)
            except ValueError:
                continue
            series[day][cat][name] = stars
    return series


def daily_gains(series):
    """算出每日每品类新增的 star。

    关键：只统计**两天都在榜**的项目。否则新项目进场会被算成凭空暴涨，
    掉出榜单的项目又会让总数莫名下降 —— 那样出来的趋势是假的。
    """
    dates = sorted(series)
    gains = defaultdict(dict)  # date -> category -> 新增 star
    for before_day, after_day in zip(dates, dates[1:]):
        before_cats = series[before_day]
        after_cats = series[after_day]
        for cat in set(before_cats) | set(after_cats):
            before = before_cats.get(cat, {})
            after = after_cats.get(cat, {})
            common = set(before) & set(after)
            if common:
                gains[after_day][cat] = sum(after[n] - before[n] for n in common)
    return gains, dates


def setup_font(mpl, lang):
    """中文需要专门找 CJK 字体；英文用 matplotlib 自带的就够。"""
    if lang != "zh":
        mpl.rcParams["axes.unicode_minus"] = False
        return "DejaVu Sans (default)"

    from matplotlib import font_manager
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in CJK_FONTS:
        if name in available:
            mpl.rcParams["font.family"] = name
            mpl.rcParams["axes.unicode_minus"] = False  # 负号显示成方块的老问题
            return name
    return None


def style_axes(ax, show_grid=True):
    """统一处理坐标轴样式：去掉上右边框，网格用发丝实线。"""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(BASELINE)
        ax.spines[side].set_linewidth(0.8)
    ax.set_facecolor(SURFACE)
    ax.tick_params(colors=INK_MUTED, labelsize=8, length=0)
    if show_grid:
        # 实线，不是虚线 —— 虚线会被误读成"预测"或"阈值"
        ax.grid(True, color=GRID, linewidth=0.6, linestyle="-", zorder=0)
        ax.set_axisbelow(True)


def label_for(cat, names, lang):
    return names.get(cat, cat) if lang == "en" else cat


def draw_trend(mpl, plt, gains, dates, names, lang):
    """主图：小倍数折线，每格一个品类。"""
    S = STRINGS[lang]
    plot_days = dates[1:]  # 第一天没有对比基线
    cats = sorted({c for d in plot_days for c in gains[d]})

    # 统一 y 轴刻度，否则各格尺度不同，看着没法横向比较
    peak = max((gains[d].get(c, 0) for d in plot_days for c in cats), default=0)
    y_max = max(peak * 1.15, 1)

    ncols = 4
    nrows = -(-len(cats) // ncols)  # 向上取整
    fig, axes = plt.subplots(nrows, ncols, figsize=(14, 2.6 * nrows),
                             facecolor=SURFACE, sharey=True)
    axes = axes.flatten()

    x = list(range(len(plot_days)))
    for i, cat in enumerate(cats):
        ax = axes[i]
        y = [gains[d].get(cat) for d in plot_days]
        xs = [xi for xi, v in zip(x, y) if v is not None]
        ys = [v for v in y if v is not None]

        if xs:
            ax.plot(xs, ys, color=SERIES, linewidth=2, marker="o", markersize=4,
                    markerfacecolor=SERIES, markeredgecolor=SURFACE,
                    markeredgewidth=1.2, zorder=3, clip_on=False)
            # 只标终点，不给每个点都写数字
            ax.annotate(f"{ys[-1]:,}", (xs[-1], ys[-1]),
                        textcoords="offset points", xytext=(6, 0),
                        va="center", fontsize=9, color=INK)
        else:
            ax.text(0.5, 0.5, S["no_data"], ha="center", va="center",
                    transform=ax.transAxes, fontsize=9, color=INK_MUTED)

        ax.set_title(label_for(cat, names, lang), fontsize=10, color=INK,
                     pad=6, loc="left")
        ax.set_ylim(0, y_max)
        style_axes(ax, show_grid=True)

        ticks = x[:: max(1, len(x) // 4)]
        ax.set_xticks(ticks)
        ax.set_xticklabels([plot_days[t][5:] for t in ticks], fontsize=8)

    for j in range(len(cats), len(axes)):
        axes[j].axis("off")

    fig.suptitle(S["trend_title"], fontsize=13, color=INK, x=0.005, ha="left", y=0.995)
    fig.tight_layout(rect=(0, 0, 1, 0.98))
    return fig


def draw_scale(mpl, plt, series, names, lang):
    """降级图：数据不足 2 天时，画当前各品类规模。"""
    S = STRINGS[lang]
    latest = sorted(series)[-1]
    stats = []
    for cat, repos in series[latest].items():
        stars = sorted(repos.values())
        mid = stars[len(stars) // 2]
        stats.append((label_for(cat, names, lang), mid))
    stats.sort(key=lambda s: s[1])

    fig, ax = plt.subplots(figsize=(9, 0.45 * len(stats) + 1.6), facecolor=SURFACE)
    ax.barh([s[0] for s in stats], [s[1] for s in stats],
            color=SERIES, height=0.62, zorder=3)

    for i, (_, v) in enumerate(stats):
        ax.annotate(f"{v:,}", (v, i), textcoords="offset points", xytext=(6, 0),
                    va="center", fontsize=9, color=INK)

    ax.set_title(S["scale_title"].format(date=latest, n=1),
                 fontsize=12, color=INK, pad=12, loc="left")
    ax.set_xlim(0, max(s[1] for s in stats) * 1.18)
    style_axes(ax, show_grid=True)
    ax.grid(axis="y", visible=False)  # 横向条不需要横网格
    fig.tight_layout()
    return fig


def write_table(gains, dates):
    """图的表格版兄弟 —— 保证数值不只靠图形传达。"""
    plot_days = dates[1:]
    cats = sorted({c for d in plot_days for c in gains[d]})
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date"] + cats)
        for d in plot_days:
            w.writerow([d] + [gains[d].get(c, "") for c in cats])


def main():
    parser = argparse.ArgumentParser(description="生成趋势图")
    parser.add_argument("--lang", choices=["zh", "en", "both"], default="both",
                        help="出哪个语言版本（默认两个都出）")
    args = parser.parse_args()

    try:
        import matplotlib
        matplotlib.use("Agg")  # 无界面环境（CI）必须
        import matplotlib.pyplot as plt
    except ImportError:
        print("没装 matplotlib，跳过画图。需要的话：pip install matplotlib")
        return 0

    series = load_history()
    gains, dates = daily_gains(series)
    names = load_names()
    langs = ["zh", "en"] if args.lang == "both" else [args.lang]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    for lang in langs:
        font = setup_font(matplotlib, lang)
        if lang == "zh":
            print(f"中文字体: {font or '未找到（中文可能显示为方块）'}")

        if len(dates) >= 2:
            fig = draw_trend(matplotlib, plt, gains, dates, names, lang)
        else:
            fig = draw_scale(matplotlib, plt, series, names, lang)

        out = REPORT_DIR / f"trend.{lang}.png"
        fig.savefig(out, dpi=140, facecolor=SURFACE, bbox_inches="tight")
        plt.close(fig)
        print(f"已保存: {out}")

    if len(dates) >= 2:
        write_table(gains, dates)
    else:
        print(f"只有 {len(dates)} 天数据，趋势图需要 ≥2 天，本次画的是当前规模。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
