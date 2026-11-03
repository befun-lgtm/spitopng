#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
st7735_decode.py — 从逻辑分析仪 SPI 抓包还原 ST7735S 屏幕截图
================================================================

场景
----
单片机用 4 线 SPI 驱动 ST7735S（160x128，RGB565）。不接屏，逻辑分析仪并联在
FPC 总线上，抓 SCL / SDA(MOSI) / CSX / DCX 四根线（RESX 不参与像素还原）。

核心设计
--------
* **流式单遍解析**：不把波形按 CS 段分别出图。CS 只用来切"事务"（transaction），
  命令/参数的解析器是一个**跨事务状态机**：0x2A/0x2B/0x2C/0x36 攒起来，
  像素字节持续累加进当前帧的 framebuffer，直到出现新的命令字节（DC=0）才收帧。
  所以"一帧 20480 像素被拆成几十个 CS 事务"也能正确拼回来。
* **时间列不参与计算**：SPI 时钟由"每个 bit 的采样点数"反推
  （f_spi = f_sample / samples_per_bit），不受导出时间单位（s/ms/ns/样本号）影响。
* **MADCTL 参与映射**：MV/MX/MY 决定逻辑地址 → 物理坐标的变换，bit3 决定 RGB/BGR。
  两者都能用命令行强制覆盖。

用法（最短）
------------
    python st7735_decode.py capture.csv -o shot.png

完整示例
--------
    python st7735_decode.py capture.csv --sample-rate 24e6 ^
        --spi-mode 0 --bit-order msb --cs-active low --dc-command low ^
        --width 160 --height 128 --offset-x 0 --offset-y 0 ^
        --byte-order big --madctl auto --color-order auto -o shot.png

    # PulseView 里通道名不是 scl/sda/csx/dcx 时，按名字指定
    python st7735_decode.py capture.csv --scl-col SCLK --sda-col MOSI --cs-col CS --dc-col A0

    # 明确按列号指定（0 起），忽略表头
    python st7735_decode.py capture.csv --columns 0,1,2,3,4 --header no

    # 确认偏移不对时手动试
    python st7735_decode.py capture.csv --offset-x 2 --offset-y 1 -o shot.png

依赖
----
Python 3.8+；Pillow 可选（无 Pillow 时输出 PPM，可再另存 PNG）。

注意
----
抓包前提：采样率 >= 4 x SPI 时钟。24MHz 采样时 SPI 不要超过 6MHz，否则边沿会被漏采。
"""

from __future__ import annotations

import argparse
import bisect
import csv
import math
import os
import re
import sys
from itertools import islice
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

# ----------------------------------------------------------------------------
# Pillow 可选
# ----------------------------------------------------------------------------
try:
    from PIL import Image  # type: ignore
    HAVE_PIL = True
except Exception:  # pragma: no cover
    Image = None  # type: ignore
    HAVE_PIL = False


# ============================================================================
# 1. 电平 / 列名 / 采样源
# ============================================================================

_TRUE = ("1", "true", "h", "high", "on")
_FALSE = ("0", "false", "l", "low", "off")


def norm_level(tok: str) -> Optional[int]:
    """把 CSV/VCD 里的电平文本归一成 0/1；无法识别返回 None。

    兼容：0/1、0.0/1.0、3.3/0.0（模拟电压，>0.5V 视为高）、true/false、HIGH/LOW。
    """
    if tok is None:
        return None
    t = tok.strip().strip('"').strip("'").strip()
    if not t:
        return None
    tl = t.lower()
    if tl in _TRUE:
        return 1
    if tl in _FALSE:
        return 0
    try:
        return 1 if float(tl) > 0.5 else 0
    except ValueError:
        return None


def norm_name(s: str) -> str:
    """列名归一：小写 + 只保留字母数字，'CS_N'/'cs-x'/'CSX' 都能对上。"""
    return "".join(ch for ch in str(s).strip().lower() if ch.isalnum())


# 每个通道可接受的表头别名（改成你自己的名字也可以）
ALIASES: Dict[str, Tuple[str, ...]] = {
    "time": ("time", "t", "timestamp", "times", "sample", "samples", "index", "no"),
    "scl": ("scl", "sck", "sclk", "spiclk", "clk", "clock", "spi_clk", "d0", "ch0", "channel0"),
    "sda": ("sda", "mosi", "sdi", "din", "data", "d1", "ch1", "channel1", "mosi_sda"),
    "cs": ("cs", "csx", "csn", "ncs", "ss", "ssn", "csb", "d2", "ch2", "channel2"),
    "dc": ("dc", "dcx", "a0", "rs", "dcs", "d_c", "d3", "ch3", "channel3"),
}

CHANNELS = ("time", "scl", "sda", "cs", "dc")
DEFAULT_INDEX = {"time": 0, "scl": 1, "sda": 2, "cs": 3, "dc": 4}


def resolve_column(spec: Optional[str], header: Optional[Sequence[str]],
                   channel: str, fallback: int) -> Optional[int]:
    """把 "3" / "SCLK" / None 解析成列下标。"""
    if spec is None or str(spec).strip() == "":
        # 没指定：有表头就先按别名找，否则用默认位置
        if header:
            for i, name in enumerate(header):
                if norm_name(name) in [norm_name(a) for a in ALIASES[channel]]:
                    return i
        return fallback
    s = str(spec).strip()
    if s.isdigit():
        return int(s)
    if not header:
        raise SystemExit("错误：指定了列名 %r，但文件没有表头。请改用列号，或加 --header yes。" % s)
    n = norm_name(s)
    for i, name in enumerate(header):
        if norm_name(name) == n:
            return i
    # 模糊：包含关系
    for i, name in enumerate(header):
        hn = norm_name(name)
        if n and (n in hn or (hn and hn in n)):
            return i
    # 再退一步：别名（用户名字可能和表头不是一个写法，这里兜底并提示）
    al = [norm_name(a) for a in ALIASES[channel]]
    for i, name in enumerate(header):
        if norm_name(name) in al:
            print("[注意] --%s-col %r 没有匹配到表头，按别名改用第 %d 列 %r"
                  % (channel, s, i, header[i]), file=sys.stderr)
            return i
    raise SystemExit("错误：表头 %r 里找不到列 %r（可用列：%s）"
                     % (list(header), s, ", ".join(header)))


def build_column_map(args, header: Optional[Sequence[str]]) -> Dict[str, Optional[int]]:
    """确定 5 个逻辑通道各取第几列。--columns 优先，其次 --xxx-col，最后默认位置。"""
    specs: Dict[str, Optional[str]] = {ch: None for ch in CHANNELS}
    if args.columns:
        parts = [p.strip() for p in args.columns.split(",")]
        for i, ch in enumerate(CHANNELS):
            if i < len(parts) and parts[i]:
                specs[ch] = parts[i]
    else:
        specs["time"] = args.time_col
        specs["scl"] = args.scl_col
        specs["sda"] = args.sda_col
        specs["cs"] = args.cs_col
        specs["dc"] = args.dc_col
    return {ch: resolve_column(specs[ch], header, ch, DEFAULT_INDEX[ch]) for ch in CHANNELS}



# ============================================================================
# 1b. PulseView / sigrok 的 8 通道边沿表（"Time [s],Channel 0..7"）
#     这种文件只记录"变化时刻"，没有固定采样点，所以按时间戳当样本回放。
# ============================================================================

_PV_RE = re.compile(r"^channel\s*(\d+)$", re.I)


def is_pv_edge_header(header: Optional[Sequence[str]]) -> bool:
    """表头形如 ["Time [s]","Channel 0",...] 就认定是 PulseView 边沿表。"""
    if not header or len(header) < 3:
        return False
    if not norm_name(header[0]).startswith("time"):
        return False
    hits = sum(1 for h in header[1:] if _PV_RE.match(str(h).strip()))
    return hits >= 2


def _fit_sample_period(diffs: List[int]) -> float:
    """用"所有边沿间隔都是采样周期的整数倍"这一事实反推采样周期(ns)。

    取最小间隔当周期是错的：相邻两行边沿本身就可能隔好几个采样点。
    这里在小范围内暴力拟合，残差最小的那个周期就是采样周期。
    """
    if not diffs:
        return 0.0
    d = diffs[:6000]
    cands = []
    p = 10.0
    while p <= 700.0:
        hit = 0
        for x in d:
            n = int(round(x / p))
            if n >= 1 and abs(x - n * p) <= 1.6:
                hit += 1
        cands.append((hit, p))
        p += 0.25
    best_hit = max(h for h, _ in cands)
    # 周期的整数分之一同样能"拟合"（12.5 与 62.5 都对），所以取仍能拿满分的最大周期
    ok = [q for h, q in cands if h >= best_hit * 0.995]
    return max(ok) if ok else 0.0


def _scan_pv_activity(path: str, nch: int):
    """第一遍：每路翻转次数、首末边沿、时间网格。"""
    tog = [0] * nch
    first = [None] * nch
    last = [None] * nch
    prev = None
    t0 = t1 = None
    grid = 0
    prev_t_ns = None
    min_step = None
    diffs: List[int] = []
    with open(path, newline="") as f:
        rd = csv.reader(f)
        next(rd, None)
        for row in rd:
            if len(row) < nch + 1:
                continue
            try:
                t = float(row[0])
            except ValueError:
                continue
            v = [1 if row[i + 1].strip() not in ("0", "0.0", "") else 0 for i in range(nch)]
            t_ns = int(round(t * 1e9))
            if prev_t_ns is not None and t_ns > prev_t_ns:
                d = t_ns - prev_t_ns
                grid = math.gcd(grid, d)
                if min_step is None or d < min_step:
                    min_step = d
                if len(diffs) < 20000:
                    diffs.append(d)
            prev_t_ns = t_ns
            t0 = t if t0 is None else t0
            t1 = t
            if prev is None:
                prev = v
                continue
            for i in range(nch):
                if v[i] != prev[i]:
                    tog[i] += 1
                    if first[i] is None:
                        first[i] = t
                    last[i] = t
            prev = v
    return tog, first, last, t0, t1, grid, min_step, diffs



def _pv_channel_profile(path: str, nch: int):
    """一遍扫完，返回每通道的边沿时间表 + 有序段时长 + 统计量。

    统计量里最关键的两个：
      duty  —— 占空比。真 SPI 时钟一定接近 50%。
      reg   —— 周期规律性（全周期落在中位值 ±10% 内的比例）。
               真时钟在事务内部严格恒频，接近 100%。
    """
    edges = {c: [] for c in range(nch)}
    dur = {c: [] for c in range(nch)}      # 有序 (电平, 时长)
    cur = None
    t_first = t_last = None
    prev_t = [0.0] * nch
    with open(path, newline="") as f:
        rd = csv.reader(f)
        next(rd, None)
        for row in rd:
            if len(row) < nch + 1:
                continue
            try:
                t = float(row[0])
                v = [1 if float(x) >= 0.5 else 0 for x in row[1:nch + 1]]
            except ValueError:
                continue
            if cur is None:
                cur = v
                t_first = t_last = t
                prev_t = [t] * nch
                for c in range(nch):
                    edges[c].append((t, v[c]))
                continue
            t_last = t
            for c in range(nch):
                if v[c] != cur[c]:
                    edges[c].append((t, v[c]))
                    dur[c].append((cur[c], t - prev_t[c]))
                    prev_t[c] = t
            cur = v
    for c in range(nch):
        if t_last is not None:
            dur[c].append((cur[c], t_last - prev_t[c]))

    stat = {}
    for c in range(nch):
        d = dur[c]
        hi = sorted(x for lv, x in d if lv == 1)
        lo = sorted(x for lv, x in d if lv == 0)
        if not hi or not lo:
            stat[c] = dict(ok=False, hi=hi, lo=lo, n=len(edges[c]))
            continue
        mh, ml = hi[len(hi) // 2], lo[len(lo) // 2]
        pers = sorted(d[i][1] + d[i + 1][1] for i in range(len(d) - 1))
        mp = pers[len(pers) // 2] if pers else 0
        reg = (sum(1 for x in pers if abs(x - mp) <= 0.10 * mp) / len(pers)) if pers else 0.0
        stat[c] = dict(ok=True, hi=hi, lo=lo, n=len(edges[c]), mh=mh, ml=ml,
                       duty=mh / (mh + ml) if (mh + ml) else 0.0,
                       period=mh + ml, reg=reg)
    return edges, stat, t_first, t_last


def _phase_conc(ce: List[float], de: List[float], nbins: int = 12):
    """把数据边沿换算成"距上一个时钟边沿的相位"，返回最集中的 bin 占比。

    真总线：MOSI 只在时钟周期的固定相位翻转 → 集中度接近 100%。
    无关信号：相位均匀分布 → 约 1/nbins。
    """
    if len(ce) < 30 or len(de) < 30:
        return None
    diffs = sorted(ce[i + 1] - ce[i] for i in range(len(ce) - 1))
    h = diffs[len(diffs) // 2]
    if h <= 0:
        return None
    hist = [0] * nbins
    n = 0
    for t in de:
        i = bisect.bisect_left(ce, t) - 1
        if i < 0:
            continue
        ph = (t - ce[i]) / h
        if ph > 1.5:          # 时钟静默期，不计入
            continue
        hist[min(nbins - 1, int(ph * nbins))] += 1
        n += 1
    if n < 30:
        return None
    peak = max(hist)
    return peak / n, hist.index(peak), n


def _fmt_dur(d: float) -> str:
    if d >= 1e-3:
        return "%.1f ms" % (d * 1e3)
    if d >= 1e-6:
        return "%.2f us" % (d * 1e6)
    return "%.0f ns" % (d * 1e9)


def _describe_channel(st: dict) -> str:
    """根据统计量给出一句人话描述。"""
    if not st.get("ok"):
        return "全程无变化（DC 电平线）"
    mh, ml, per, duty = st["mh"], st["ml"], st["period"], st["duty"]
    if per <= 0:
        return "无翻转"
    ratio = max(mh, ml) / min(mh, ml) if min(mh, ml) > 0 else 1e9
    if ratio > 4 and min(mh, ml) < 1e-6:
        return "窄脉冲 %s 宽、每 %s 一个 → 开关电源 / 耦合尖峰" % (
            _fmt_dur(min(mh, ml)), _fmt_dur(max(mh, ml)))
    if per > 50e-6:
        return "低频方波 %.2f kHz，占空比 %.0f%% → 背光 / 电源 PWM" % (1.0 / per / 1e3, duty * 100)
    return "周期约 %s，占空比 %.0f%%，周期稳定性 %.0f%%" % (_fmt_dur(per), duty * 100, st["reg"] * 100)


def _find_duplicate_nodes(times: Dict[int, List[float]], tol: float = 150e-9,
                          need: float = 0.90):
    """找出"其实是同一根线"的通道对（夹子压住了两个焊盘 / 两针被短路）。

    判据：一方的边沿有 ≥need 比例落在另一方的边沿 ±tol 内。
    """
    out = []
    chs = [c for c in times if len(times[c]) > 30]
    for i, c in enumerate(chs):
        for d in chs[i + 1:]:
            a, b = times[c], times[d]
            if len(a) > len(b) * 4 or len(b) > len(a) * 4:
                continue
            hit = 0
            for t in a[::max(1, len(a) // 4000)]:
                j = bisect.bisect_left(b, t)
                ok = False
                for k in (j - 1, j, j + 1):
                    if 0 <= k < len(b) and abs(b[k] - t) <= tol:
                        ok = True
                        break
                hit += ok
            n = len(a[::max(1, len(a) // 4000)])
            if n and hit / n >= need:
                out.append((c, d, hit / n))
    return out


def _pv_bus_verdict(path: str, nch: int, quiet: bool = False):
    """总线真伪判定：返回 ((best_c, best_d, conc), lines)。

    判断"抓到的是不是 SPI 总线"：
      1) 先剔除"同一根线"的重复通道（夹子太宽压住两个焊盘时会这样）
      2) 时钟资格：占空比 15~85%（软件位翻转的时钟本来就不对称，不能要求 50%）
      3) 相位对齐：MOSI 边沿必须集中在时钟周期的同一相位（>75%）
    只靠第 3 条会误判：开关电源的基波与谐波之间集中度也是 100%。
    """
    edges, stat, t_first, t_last = _pv_channel_profile(path, nch)
    active = [c for c in range(nch) if stat[c]["n"] > 30]
    times = {c: [t for t, _ in edges[c]] for c in active}
    L: List[str] = []

    dups = _find_duplicate_nodes(times)
    dup_chs = set()
    for c, d, f in dups:
        dup_chs.add(d)
        L.append("")
        L.append("  !! Channel %d 和 Channel %d 是同一根线（%.0f%% 的边沿完全重合）"
                 % (c, d, f * 100))
        L.append("     → 两个夹子压在同一个焊盘上，或者这两针被夹子短路了，白废一个通道")

    L.append("")
    L.append("--- 通道体检（判定抓到的是不是 SPI 总线）---")
    L.append("%-7s %9s %11s %11s %7s %8s  %s"
             % ("通道", "翻转数", "高电平中位", "低电平中位", "占空比", "周期稳定", "性质"))
    for c in range(nch):
        st = stat[c]
        pre = "%-7s %9d " % ("Ch%-5d" % c, st["n"])
        if not st.get("ok"):
            L.append(pre + "%11s %11s %7s %8s  %s" % ("-", "-", "-", "-", _describe_channel(st)))
            continue
        tail = _describe_channel(st)
        if c in dup_chs:
            tail += "（与上一路重复）"
        L.append(pre + "%11s %11s %6.0f%% %7.0f%%  %s" % (
            _fmt_dur(st["mh"]), _fmt_dur(st["ml"]), st["duty"] * 100,
            st["reg"] * 100, tail))

    # 时钟资格：只要占空比不离谱即可（软件位翻转时钟周期天然不稳）
    clk_ok = [c for c in active if stat[c].get("ok") and 0.15 <= stat[c]["duty"] <= 0.85]
    best = (None, None, 0.0)
    rows = []
    for c in active:
        for d in active:
            if c == d or c in dup_chs or d in dup_chs:
                continue
            if c not in clk_ok:
                continue
            # 数据线每周期最多翻一次 → 边沿数不应超过时钟边沿数
            if len(times[d]) > len(times[c]) * 0.9:
                continue
            r = _phase_conc(times[c], times[d])
            if r:
                rows.append((c, d, r[0], r[2]))
                if r[0] > best[2]:
                    best = (c, d, r[0])
    rows.sort(key=lambda x: -x[2])
    L.append("")
    L.append("  时钟/数据 配对（相位集中度 >75% 才算同一总线）")
    for c, d, conc, n in rows[:4]:
        L.append("    Ch%d(时钟) + Ch%d(数据)   集中度 %5.1f%%   (%d 个边沿)"
                 % (c, d, conc * 100, n))
    if not rows:
        L.append("    没有可配对的通道（活跃通道太少，或都判成重复/电源）")
    if not clk_ok:
        L.append("    !! 没有任何一路具备时钟特征（占空比 15~85%）")
    if not quiet:
        for s in L:
            print(s)
    return best, L



def _pv_clock_edge_levels(path: str, nch: int, scl: int, cands: List[int]) -> Dict[int, Tuple[int, int]]:
    """统计 SCL 每个边沿发生时，候选通道各处于什么电平。

    真正的 CS：时钟边沿只会出现在它"有效"的那个电平上（片选无效时时钟是静止的）。
    DC 没有这个性质（DC 会在很多个时钟周期里保持不变，两侧都有时钟边沿）。
    """
    cnt = {c: [0, 0] for c in cands}
    prev = None
    with open(path, newline="") as f:
        rd = csv.reader(f)
        next(rd, None)
        for row in rd:
            if len(row) < nch + 1:
                continue
            try:
                v = [1 if float(x) >= 0.5 else 0 for x in row[1:nch + 1]]
            except ValueError:
                continue
            if prev is not None and v[scl] != prev[scl]:
                for c in cands:
                    cnt[c][v[c]] += 1
            prev = v
    return {c: (v[0], v[1]) for c, v in cnt.items()}


def _pick_pv_roles(tog, args, log: List[str]) -> Dict[str, int]:
    """按翻转次数挑 SCL/SDA，剩下的里挑 CS/DC；可用 --scl-col 等手工覆盖（写通道号）。"""
    nch = len(tog)
    active = [i for i in range(nch) if tog[i] > 0]

    def explicit(name: str, default: int) -> int:
        spec = getattr(args, name, None)
        if spec is None:
            return default
        s = str(spec).strip()
        if s.isdigit() and int(s) < nch:
            return int(s)
        if s.startswith("channel") or s.lower().startswith("ch"):
            m = re.search(r"(\d+)", s)
            if m and int(m.group(1)) < nch:
                return int(m.group(1))
        return default

    ranked = sorted(active, key=lambda i: -tog[i])
    scl = ranked[0] if ranked else 0
    sda = ranked[1] if len(ranked) > 1 else scl
    rest = [i for i in range(nch) if i not in (scl, sda)]
    rest_active = sorted([i for i in rest if tog[i] > 0], key=lambda i: tog[i])
    cs = rest_active[0] if rest_active else (rest[0] if rest else 0)
    dc = rest_active[1] if len(rest_active) > 1 else next(
        (i for i in rest if i != cs), cs)

    scl = explicit("scl_col", scl)
    sda = explicit("sda_col", sda)
    cs = explicit("cs_col", cs)
    dc = explicit("dc_col", dc)

    if not rest_active:
        log.append("!! 除 SCL/SDA 外没有任何通道在动 → CS 和 DC 没接上（或没被采到）")
        setattr(args, "_no_csdc", True)
    return {"scl": scl, "sda": sda, "cs": cs, "dc": dc}


def _pv_edge_alignment(path: str, nch: int, scl: int, cands: List[int],
                       tol: float = 200e-6) -> Dict[int, float]:
    """每个候选通道的边沿里，有多少比例紧邻时钟边沿（±tol）。

    真正的 CS/DC 一定贴着时钟动作（CS 在事务边界、DC 在字节边界）；
    接在电源脚/无关引脚上的通道只会捡到耦合毛刺，比例极低。
    """
    clk: List[float] = []
    chg: Dict[int, List[float]] = {c: [] for c in cands}
    prev = None
    with open(path, newline="") as f:
        rd = csv.reader(f)
        next(rd, None)
        for row in rd:
            if len(row) < nch + 1:
                continue
            try:
                vs = [1 if float(x) >= 0.5 else 0 for x in row[1:nch + 1]]
                t = float(row[0])
            except ValueError:
                continue
            if prev is not None and vs[scl] != prev[scl]:
                clk.append(t)
            for c in cands:
                if prev is not None and vs[c] != prev[c]:
                    chg[c].append(t)
            prev = vs
    out: Dict[int, float] = {}
    if not clk:
        return {c: 0.0 for c in cands}
    for c in cands:
        ts = chg[c]
        if not ts:
            out[c] = 0.0
            continue
        near = 0
        for t in ts:
            k = bisect.bisect_left(clk, t - tol)
            if k < len(clk) and abs(clk[k] - t) <= tol:
                near += 1
        out[c] = near / len(ts)
    return out


def iter_pv_edge_samples(path: str, args) -> Iterator[Tuple[float, int, int, int, int]]:
    """把 8 通道边沿表回放成 (t, scl, sda, cs, dc) 样本序列。

    通道角色判定顺序：
      1) 翻转最多的两路 = SCL / SDA（时钟一定最快）
      2) 在剩下的活动通道里，用"时钟边沿只落在它有效的那一侧"挑出 CS（这条最可靠）
      3) 再剩下的就是 DC
    想手工指定就写 --scl-col 3 --sda-col 1 --cs-col 2 --dc-col 4（填通道号）。
    """
    with open(path, newline="") as f:
        head = next(csv.reader(f), [])
    nch = sum(1 for h in head[1:] if _PV_RE.match(str(h).strip()))
    tog, first, last, t0, t1, grid, min_step, diffs = _scan_pv_activity(path, nch)

    log: List[str] = []
    roles = _pick_pv_roles(tog, args, log)
    isc, isd = roles["scl"], roles["sda"]
    icc, icd = roles["cs"], roles["dc"]

    # ---- 硬判据优先：相位集中度高的一对才是真的 SCL/MOSI ----
    (b_c, b_d, b_conc), verdict_lines = _pv_bus_verdict(path, nch, quiet=True)
    setattr(args, "_bus_conc", b_conc)
    if b_conc >= 0.75 and getattr(args, "scl_col", None) is None \
            and getattr(args, "sda_col", None) is None:
        if (isc, isd) != (b_c, b_d):
            log.append("相位证据改判：SCL=Channel %d，MOSI=Channel %d（原按翻转数判成 %d/%d）"
                       % (b_c, b_d, isc, isd))
        isc, isd = b_c, b_d
    elif b_conc < 0.60:
        setattr(args, "_not_a_bus", True)

    # ---- CS 精判：时钟边沿只出现在片选有效的那一侧 ----
    cands = [i for i in range(nch) if i not in (isc, isd) and tog[i] > 0]
    cs_reason = ""
    if cands:
        lv = _pv_clock_edge_levels(path, nch, isc, cands)

        def skew(c):
            a, b = lv[c]
            t = a + b
            return (max(a, b) / t) if t else 0.0

        cands.sort(key=lambda c: -skew(c))
        icc = cands[0]
        icd = cands[1] if len(cands) > 1 else cands[0]
        a, b = lv[icc]
        args.cs_active = "low" if a > b else "high"
        cs_reason = ("CS = Channel %d（%.1f%% 的时钟边沿落在 CS=%d 一侧），DC = Channel %d"
                     % (icc, 100.0 * skew(icc), 0 if a > b else 1, icd))

    # ---- 相关性自检：CS/DC 必须贴着时钟动作 ----
    suspect: List[str] = []
    if cands:
        rel = _pv_edge_alignment(path, nch, isc, [icc, icd])
        for c, name in ((icc, "CS"), (icd, "DC")):
            r = rel.get(c, 0.0)
            if tog[c] and r < 0.20:
                suspect.append(
                    "Channel %d 被当成 %s，但它的边沿只有 %.0f%% 紧邻时钟，"
                    "很可能没接在 %s 上（电源脚/无关引脚/悬空耦合）"
                    % (c, name, 100 * r, name))
        if not tog[icc] or not tog[icd]:
            suspect.append("CS 或 DC 对应通道全程无翻转")

    # ---- 打印体检表 ----
    period = _fit_sample_period(diffs)
    if period:
        fs = 1000.0 / period
        log.append("采样网格 %.2f ns → 实际采样率约 %.1f MHz（最小边沿间隔 %d ns）%s"
                   % (period, fs, min_step or 0,
                      "" if fs >= 8 else "  !! 偏低，建议调到 12~24MHz"))
    if t0 is not None:
        log.append("抓包跨度 %.3f s（首边沿 %.6f s，末边沿 %.6f s）" % (t1 - t0, t0, t1))
    if cs_reason:
        log.append(cs_reason)
    if getattr(args, "_pv_log_shown", False) is False:
        print()
        print("PulseView 8 通道边沿表：")
        for line in log:
            print("  " + line)
        for line in verdict_lines:
            print(line)
        if getattr(args, "_not_a_bus", False):
            print("\n  通道角色（无总线可判，下面只是强行指定，结果不可信）：")
        else:
            print("\n  通道角色：")
        for i in range(nch):
            role = ("SCL" if i == isc else "SDA" if i == isd else
                    "CS" if i == icc else "DC" if i == icd else "-")
            mark = "" if tog[i] else "   (全程无变化)"
            print("     Channel %d  翻转 %8d  %-3s%s" % (i, tog[i], role, mark))
        for line in suspect:
            print("  !! " + line)
        setattr(args, "_pv_log_shown", True)

    # ---- CS/DC 缺席：把 CS 当成"一直有效"，至少能统计出解了多少字节 ----
    cs_force = None
    if getattr(args, "_no_csdc", False):
        cs_force = 0 if args.cs_active == "low" else 1

    with open(path, newline="") as f:
        rd = csv.reader(f)
        next(rd, None)
        for row in rd:
            if len(row) < nch + 1:
                continue
            try:
                t = float(row[0])
                vs = [1 if float(x) >= 0.5 else 0 for x in row[1:nch + 1]]
            except ValueError:
                continue
            yield (t, vs[isc], vs[isd],
                   cs_force if cs_force is not None else vs[icc], vs[icd])


def iter_csv_samples(path: str, args) -> Iterator[Tuple[float, int, int, int, int]]:
    """通用 CSV 采样源：逐行产出 (time, scl, sda, cs, dc)。

    表头自动识别（第一行出现非数字 token 就当表头）。
    """
    idx: Optional[Dict[str, Optional[int]]] = None
    header: Optional[List[str]] = None
    data_rows: List[List[str]] = []

    with open(path, "r", newline="", encoding=args.encoding, errors="replace") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or all(not c.strip() for c in row):
                continue
            data_rows.append(row)
            break
        if not data_rows:
            raise SystemExit("错误：%s 是空文件" % path)
        first = data_rows[0]

        # ---- 表头判定 ----
        if args.header == "yes":
            is_header = True
        elif args.header == "no":
            is_header = False
        else:
            is_header = any(
                (tok.strip() != "" and norm_level(tok) is None) for tok in first
            )
        if is_header:
            header = [c.strip() for c in first]
        else:
            header = None

        idx = build_column_map(args, header)
        need = max(v for v in idx.values() if v is not None)

        def rows() -> Iterator[List[str]]:
            if not is_header:
                yield first
            for r in reader:
                if not r or all(not c.strip() for c in r):
                    continue
                yield r

        bad = 0
        for r in rows():
            if len(r) <= need:
                bad += 1
                if bad == 1:
                    print("[警告] CSV 有行列数不足，已跳过（后续同类不再提示）")
                continue
            scl = norm_level(r[idx["scl"]]) if idx["scl"] is not None else None
            sda = norm_level(r[idx["sda"]]) if idx["sda"] is not None else None
            cs = norm_level(r[idx["cs"]]) if idx["cs"] is not None else None
            dc = norm_level(r[idx["dc"]]) if idx["dc"] is not None else None
            if scl is None or sda is None or cs is None or dc is None:
                continue
            t = 0.0
            if idx["time"] is not None:
                try:
                    t = float(r[idx["time"]].strip().strip('"'))
                except (ValueError, TypeError):
                    t = 0.0
            yield (t, scl, sda, cs, dc)


def iter_vcd_samples(path: str, args) -> Iterator[Tuple[float, int, int, int, int]]:
    """最小可用的 VCD 采样源（只支持 1bit scalar，够 SPI 用）。

    VCD 只记录"变化点"，所以每个时间戳产出一条综合快照；解码器的边沿检测
    只看前后两拍的差异，因此仍然正确。
    """
    id_of: Dict[str, str] = {}
    cur = {"scl": 0, "sda": 0, "cs": 1, "dc": 0}
    timescale = 1.0
    in_header = True
    t = 0.0
    dirty = False
    ids_by_len: List[str] = []

    with open(path, "r", encoding=args.encoding, errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            if in_header:
                if s.startswith("$timescale"):
                    body = s[len("$timescale"):].replace("$end", "").strip()
                    body = body.lstrip("0123456789").strip()
                    timescale = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9,
                                 "ps": 1e-12, "fs": 1e-15}.get(body, 1.0)
                elif s.startswith("$var"):
                    p = s.split()
                    # $var wire 1 ! scl $end
                    if len(p) >= 5 and p[2] in ("1",):
                        code, name = p[3], p[4]
                        nn = norm_name(name)
                        for ch in ("scl", "sda", "cs", "dc"):
                            if nn in [norm_name(a) for a in ALIASES[ch]] or nn == ch:
                                id_of[ch] = code
                                break
                elif s.startswith("$enddefinitions"):
                    in_header = False
                    ids_by_len = sorted(id_of.values(), key=len, reverse=True)
                continue

            if s.startswith("#"):
                if dirty:
                    yield (t, cur["scl"], cur["sda"], cur["cs"], cur["dc"])
                    dirty = False
                try:
                    t = float(s[1:]) * timescale
                except ValueError:
                    pass
            elif s[0] in "01":
                # 紧凑写法一行可能有多个变化：0! 0" 1# 0$
                # VCD 的 id 可以是任意可打印字符，所以用"已声明的 id"做最长匹配
                pos = 0
                L = len(s)
                while pos < L:
                    ch = s[pos]
                    if ch not in "01":
                        pos += 1
                        continue
                    val = 1 if ch == "1" else 0
                    pos += 1
                    code = None
                    for c in ids_by_len:
                        if s.startswith(c, pos):
                            code = c
                            break
                    if code is None:
                        j = pos
                        while j < L and not s[j].isspace():
                            j += 1
                        code = s[pos:j]
                        pos = j
                    else:
                        pos += len(code)
                    for cch, cc in id_of.items():
                        if cc == code and cur[cch] != val:
                            cur[cch] = val
                            dirty = True
                    while pos < L and s[pos].isspace():
                        pos += 1
            # b<...> 向量、x/z 高阻：忽略
    if dirty:
        yield (t, cur["scl"], cur["sda"], cur["cs"], cur["dc"])


def open_sample_source(path: str, args) -> Iterator[Tuple[float, int, int, int, int]]:
    """格式分发：目前实现 CSV + VCD；.sr 需要 sigrok 先导出。"""
    ext = os.path.splitext(path)[1].lower()
    fmt = args.format
    if fmt == "auto":
        fmt = "vcd" if ext == ".vcd" else ("csv" if ext in (".csv", ".txt", ".tsv") else "")
    if fmt == "csv":
        # 先看是不是 PulseView 的 8 通道边沿表（Time [s],Channel 0..7）
        with open(path, newline="") as _f:
            _head = next(csv.reader(_f), [])
        if is_pv_edge_header(_head):
            return iter_pv_edge_samples(path, args), "pv"
        return iter_csv_samples(path, args), "csv"
    if fmt == "vcd":
        return iter_vcd_samples(path, args), "vcd"
    if ext == ".sr":
        raise SystemExit(
            "错误：.sr 是 sigrok 私有容器，本脚本不直接读。\n"
            "  请先用 PulseView 打开并另存为 CSV：\n"
            "    File -> Export -> Export annotation data / oder 'Export as CSV'\n"
            "  或命令行： sigrok-cli -i capture.sr -C SCL,SDA,CSX,DCX -o capture.csv"
        )
    raise SystemExit("错误：无法识别格式 %r，请用 --format csv|vcd 指定" % ext)


# ============================================================================
# 2. SPI 位流解码（时序采样 + 字节组装 + 事务切分）
# ============================================================================

@dataclass
class SpiParams:
    cpol: int = 0
    cpha: int = 0
    msb_first: bool = True
    cs_active_low: bool = True
    dc_command_low: bool = True

    @property
    def sample_on_rising(self) -> bool:
        """CPOL==CPHA 时在上升沿采样，否则下降沿。

        Mode0(0,0)->上升  Mode1(0,1)->下降  Mode2(1,0)->下降  Mode3(1,1)->上升
        """
        return self.cpol == self.cpha


class SpiDecoder:
    """逐采样点推进的 SPI 从机解码器（只看 MOSI 方向）。"""

    def __init__(self, params: SpiParams, on_byte: Callable[[int, int, int], None]):
        self.p = params
        self.on_byte = on_byte        # (value, is_data, tx_index)
        self.n_samples = 0
        self.n_bytes = 0
        self.n_cmd = 0
        self.n_data = 0
        self.tx_count = 0             # CS 事务计数
        self.warnings: List[str] = []
        self._tx_open = False
        self._cur = 0
        self._bits = 0
        self._edge_spacing: List[int] = []
        self._edge_dt: List[float] = []
        self._last_edge_at = None
        self._last_edge_time = None
        self._last_edge_tx = -1
        self._partial_lost = 0

    # ---- CS 事务 ----
    def _begin_tx(self):
        self.tx_count += 1
        self._tx_open = True
        self._cur = 0
        self._bits = 0

    def _end_tx(self):
        self._tx_open = False
        if self._bits:
            self._partial_lost += 1
            self._bits = 0
            self._cur = 0
            if len(self.warnings) < 40:
                self.warnings.append(
                    "CS 撤销时还有未凑满 8 bit 的半字节（共 %d 次），可能是漏采或时序毛刺" % self._partial_lost
                )

    # ---- 位 ----
    def _bit(self, sda: int, dc_level: int, sample_at: int):
        if self.p.msb_first:
            self._cur = ((self._cur << 1) | (1 if sda else 0)) & 0xFF
        else:
            self._cur = (self._cur >> 1) | (0x80 if sda else 0x00)
        self._bits += 1
        if self._bits == 8:
            # dc_command_low=True  : DC 低 = 命令，高 = 数据
            # dc_command_low=False : DC 高 = 命令，低 = 数据
            is_data = 1 if ((dc_level == 1) == self.p.dc_command_low) else 0
            self.n_bytes += 1
            if is_data:
                self.n_data += 1
            else:
                self.n_cmd += 1
            self.on_byte(self._cur, is_data, self.tx_count)
            self._cur = 0
            self._bits = 0

    def feed(self, samples: Iterable[Tuple[float, int, int, int, int]]) -> None:
        p = self.p
        prev_scl: Optional[int] = None
        prev_cs: Optional[bool] = None
        idx = 0
        rising_edge = p.sample_on_rising

        for (_t, scl, sda, cs, dc) in samples:
            cs_act = (cs == 0) if p.cs_active_low else (cs == 1)

            if prev_cs is None:
                prev_cs = cs_act
                prev_scl = scl
                if cs_act:
                    self._begin_tx()
                idx += 1
                continue

            if cs_act != prev_cs:
                if cs_act:
                    self._begin_tx()
                else:
                    self._end_tx()
                prev_cs = cs_act
                prev_scl = scl
                idx += 1
                continue

            if cs_act and prev_scl is not None and scl != prev_scl:
                if (scl == 1) == rising_edge:
                    # 只在"采样沿"上取位；两次采样沿的间隔 = 一个 SCL 周期
                    self._bit(sda, dc, idx)
                    if self._last_edge_tx == self.tx_count:
                        if self._last_edge_at is not None:
                            d = idx - self._last_edge_at
                            if 0 < d < 100000 and len(self._edge_spacing) < 200000:
                                self._edge_spacing.append(d)
                        # VCD 只有变化点，采样点数不代表时间，用时间戳算周期
                        if self._last_edge_time is not None:
                            dt = _t - self._last_edge_time
                            if dt > 0 and len(self._edge_dt) < 200000:
                                self._edge_dt.append(dt)
                    self._last_edge_at = idx
                    self._last_edge_time = _t
                    self._last_edge_tx = self.tx_count
            prev_scl = scl
            idx += 1

        self.n_samples = idx
        if prev_cs:
            self._end_tx()

    def median_edge_spacing(self) -> float:
        """相邻采样沿之间隔了几个采样点（仅 CSV 有意义）。"""
        if not self._edge_spacing:
            return 0.0
        s = sorted(self._edge_spacing)
        return float(s[len(s) // 2])

    def median_edge_dt(self) -> float:
        """相邻采样沿的时间差（秒），VCD 用这个反推时钟。"""
        if not self._edge_dt:
            return 0.0
        s = sorted(self._edge_dt)
        return float(s[len(s) // 2])


# ============================================================================
# 3. ST7735S 命令 / 像素状态机（跨 CS 事务累积）
# ============================================================================

CMD_NAMES = {
    0x01: "SWRESET", 0x11: "SLPOUT", 0x10: "SLPIN", 0x29: "DISPON", 0x28: "DISPOFF",
    0x2A: "CASET(列地址)", 0x2B: "RASET(行地址)", 0x2C: "RAMWR(写像素)",
    0x36: "MADCTL(方向/颜色)", 0x3A: "COLMOD(像素格式)", 0x21: "INVON", 0x20: "INVOFF",
    0x13: "NORON", 0x33: "VSCRDEF", 0x37: "VSCSAD", 0xB1: "FRMCTR1", 0xB2: "FRMCTR2",
    0xE0: "GMCTRP1", 0xE1: "GMCTRN1", 0x51: "WRDISBV", 0x53: "WRCTRLD",
}


def madctl_bits(v: int) -> str:
    """把 MADCTL 拆成人能看的样子。"""
    names = []
    for bit, nm in ((0x80, "MY(行镜像)"), (0x40, "MX(列镜像)"), (0x20, "MV(行列交换)"),
                    (0x10, "ML(扫描顺序)"), (0x08, "BGR"), (0x04, "MH")):
        if v & bit:
            names.append(nm)
    if not (v & 0x08):
        names.append("RGB")
    return "0x%02X [%s]" % (v, ", ".join(names) if names else "无")


class Frame:
    """一帧（一次 RAMWR 会话）的目标窗口 + 像素画布。"""

    def __init__(self, xs: int, xe: int, ys: int, ye: int,
                 width: int, height: int, ox: int, oy: int,
                 madctl: int, transform: bool, bgr: bool, index: int):
        self.xs, self.xe, self.ys, self.ye = xs, xe, ys, ye
        self.width, self.height = width, height
        self.ox, self.oy = ox, oy
        self.madctl = madctl
        self.transform = transform
        self.bgr = bgr
        self.index = index

        self.canvas = bytearray(width * height * 3)
        self.cover = bytearray(width * height)      # 1 = 该像素被写过
        self.cx, self.cy = xs, ys                  # 当前写入的逻辑地址
        self.pix = 0
        self.vals = []
        self.nbytes = 0
        self.overflow = 0
        self.clipped = 0
        self.txs = set()
        self.done = False
        self.vals: List[int] = []      # 原始 RGB565 值，用于字节序自检

    @property
    def expected(self) -> int:
        return max(0, (self.xe - self.xs + 1)) * max(0, (self.ye - self.ys + 1))

    def _map(self, col: int, row: int) -> Tuple[int, int]:
        """逻辑地址 -> 图像坐标（先减偏移，再按 MADCTL 做 MV/MX/MY 变换）。"""
        ix = col - self.ox
        iy = row - self.oy
        if self.transform:
            m = self.madctl
            if m & 0x20:                 # MV：行列交换（横竖屏）
                ix, iy = iy, ix
            if m & 0x40:                 # MX：列镜像
                ix = self.width - 1 - ix
            if m & 0x80:                 # MY：行镜像
                iy = self.height - 1 - iy
        return ix, iy

    def put(self, value: int, tx: int) -> None:
        """写入一个 RGB565 像素。"""
        self.txs.add(tx)
        self.nbytes += 2
        self.vals.append(value)
        if self.done:
            self.overflow += 1
            return

        r = (value >> 11) & 0x1F
        g = (value >> 5) & 0x3F
        b = value & 0x1F
        # 5/6/5 -> 8/8/8（位复制，比简单左移更平滑）
        r8 = (r << 3) | (r >> 2)
        g8 = (g << 2) | (g >> 4)
        b8 = (b << 3) | (b >> 2)
        if self.bgr:
            r8, b8 = b8, r8

        ix, iy = self._map(self.cx, self.cy)
        if 0 <= ix < self.width and 0 <= iy < self.height:
            o = iy * self.width + ix
            self.canvas[o * 3] = r8
            self.canvas[o * 3 + 1] = g8
            self.canvas[o * 3 + 2] = b8
            if not self.cover[o]:
                self.cover[o] = 1
                self.pix += 1
        else:
            self.clipped += 1

        # 地址自增（控制器行为：窗口内逐行扫描，扫完窗口自动回绕）
        self.cx += 1
        if self.cx > self.xe:
            self.cx = self.xs
            self.cy += 1
            if self.cy > self.ye:
                self.done = True

    @property
    def complete(self) -> bool:
        return self.pix >= self.expected > 0


class LcdParser:
    """ST7735S 命令流解析器。字节回调式，跨 CS 事务保持状态。"""

    def __init__(self, args):
        self.a = args
        self.force_bgr = False

        # 默认窗口 = 整块面板（含偏移）
        self.xs = args.offset_x
        self.xe = args.offset_x + args.width - 1
        self.ys = args.offset_y
        self.ye = args.offset_y + args.height - 1
        self.window_from_cmd = False

        self.madctl = 0x00
        self.madctl_seen = False
        self.colmod = 0x05
        self.colmod_seen = False

        self.cmd: Optional[int] = None
        self.params: List[int] = []
        self.cur: Optional[Frame] = None
        self.frames: List[Frame] = []
        self.frame_seq = 0

        self.n_cmd_bytes = 0
        self.n_data_bytes = 0
        self.n_ramwr = 0
        self.n_pixel_bytes = 0
        self.cmd_hist: Dict[int, int] = {}
        self.key_log: List[str] = []
        self.warnings: List[str] = []
        self._hi: Optional[int] = None

    # ---------- 工具 ----------
    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def decide_bgr(self) -> bool:
        """颜色顺序：命令行强制 > MADCTL bit3 > 默认 RGB。

        ST7735S MADCTL 的 D3 是 "RGB/BGR" 位：1 = 面板按 BGR 解释 16 位数据
        （高 5 位是蓝色），此时必须交换 R/B 才能还原真实颜色。
        """
        co = getattr(self.a, "color_order", "auto")
        if co == "bgr":
            return True
        if co == "rgb":
            return False
        if not self.madctl_seen:
            return False
        return (self.madctl & 0x08) != 0

    def _geometric_transform(self) -> bool:
        if self.a.madctl == "none":
            return False
        if getattr(self.a, "no_geom", False):
            return False
        return True

    def _mapped_madctl(self) -> int:
        if self.a.madctl in ("auto", "none"):
            return self.madctl
        try:
            return int(str(self.a.madctl), 0)
        except ValueError:
            raise SystemExit("错误：--madctl 只接受 auto / none / 0xNN")

    # ---------- 帧生命周期 ----------
    def _start_frame(self):
        m = self._mapped_madctl()
        self.force_bgr = self.decide_bgr()
        self.frame_seq += 1
        self.cur = Frame(self.xs, self.xe, self.ys, self.ye,
                         self.a.width, self.a.height,
                         self.a.offset_x, self.a.offset_y,
                         m, self._geometric_transform(), self.force_bgr, self.frame_seq)
        self._hi = None
        self.n_ramwr += 1

    def _finish_frame(self):
        if self.cur is None:
            return
        f = self.cur
        self.cur = None
        self._hi = None
        if f.pix > 0:
            self.frames.append(f)
        # 0 像素的 RAMWR 不计入帧列表，但已经算进 n_ramwr

    # ---------- 命令分发 ----------
    def on_byte(self, value: int, is_data: int, tx: int) -> None:
        if not is_data:
            # 新命令 = 上一段像素流结束
            self._finish_frame()
            self.cmd = value
            self.params = []
            self.n_cmd_bytes += 1
            self.cmd_hist[value] = self.cmd_hist.get(value, 0) + 1
            if value == 0x2C:
                self._start_frame()
                if len(self.key_log) < 200:
                    self.key_log.append("RAMWR 窗口 x=%d..%d y=%d..%d"
                                        % (self.xs, self.xe, self.ys, self.ye))
            elif value == 0x01:
                self.xs = self.a.offset_x
                self.xe = self.a.offset_x + self.a.width - 1
                self.ys = self.a.offset_y
                self.ye = self.a.offset_y + self.a.height - 1
                self.window_from_cmd = False
                if len(self.key_log) < 200:
                    self.key_log.append("SWRESET 复位窗口")
            return

        # ---- 数据字节 ----
        self.n_data_bytes += 1
        if self.cmd == 0x2C:
            self._pixel_byte(value, tx)
        else:
            self.params.append(value)
            self._apply_params()

    def _apply_params(self):
        c, p = self.cmd, self.params
        if c == 0x2A and len(p) >= 4:
            self.xs = (p[0] << 8) | p[1]
            self.xe = (p[2] << 8) | p[3]
            self.window_from_cmd = True
            if len(self.key_log) < 200:
                self.key_log.append("CASET x=%d..%d" % (self.xs, self.xe))
        elif c == 0x2B and len(p) >= 4:
            self.ys = (p[0] << 8) | p[1]
            self.ye = (p[2] << 8) | p[3]
            self.window_from_cmd = True
            if len(self.key_log) < 200:
                self.key_log.append("RASET y=%d..%d" % (self.ys, self.ye))
        elif c == 0x36 and len(p) >= 1:
            self.madctl = p[0]
            self.madctl_seen = True
            if len(self.key_log) < 200:
                self.key_log.append("MADCTL " + madctl_bits(self.madctl))
        elif c == 0x3A and len(p) >= 1:
            self.colmod = p[0]
            self.colmod_seen = True
            if len(self.key_log) < 200:
                self.key_log.append("COLMOD 0x%02X" % self.colmod)
            if self.colmod != 0x05:
                self.warn("COLMOD=0x%02X 不是 16bit(0x05)，脚本只按 RGB565 解释，结果可能异常"
                          % self.colmod)

    def _pixel_byte(self, value: int, tx: int):
        self.n_pixel_bytes += 1
        if self.cur is None:
            self._start_frame()
        if self.a.byte_order != "little":   # auto 先按大端解，随后再自检
            if self._hi is None:
                self._hi = value
            else:
                self.cur.put((self._hi << 8) | value, tx)   # type: ignore[union-attr]
                self._hi = None
        else:  # little: 低字节先到
            if self._hi is None:
                self._hi = value                    # 先到的是低字节
            else:
                self.cur.put((value << 8) | self._hi, tx)   # type: ignore[union-attr]
                self._hi = None

    def close(self):
        self._finish_frame()
        if self._hi is not None:
            self.warn("像素字节数为奇数，最后 1 字节被丢弃")
        if not self.frames:
            self.warn("没有解析到任何像素数据（没抓到 0x2C 之后的 DC=1 数据？检查 DC 极性/接线）")
        if not self.madctl_seen:
            self.warn("整段抓包里没有 0x36 MADCTL，按 MADCTL=0x00（不旋转）处理")
        if not self.colmod_seen:
            self.warn("没有 0x3A COLMOD，按 RGB565 处理")


# ============================================================================
# 4. 输出：合成 + 存图
# ============================================================================

def parse_color(s: str) -> Tuple[int, int, int]:
    s = s.strip().lstrip("#")
    if len(s) != 6:
        raise SystemExit("错误：--bg 需要 6 位十六进制，例如 000000")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def composite(frames: Sequence[Frame], width: int, height: int,
              bg: Tuple[int, int, int]) -> bytearray:
    """按时间顺序把多帧叠到一张画布上（后写的覆盖先写的）。"""
    canvas = bytearray(bytes(bg) * (width * height))
    n = width * height
    for f in frames:
        cov = f.cover
        src = f.canvas
        for i in range(n):
            if cov[i]:
                canvas[3 * i:3 * i + 3] = src[3 * i:3 * i + 3]
    return canvas


def save_png(path: str, width: int, height: int, rgb: bytearray, args) -> str:
    if HAVE_PIL:
        img = Image.frombytes("RGB", (width, height), bytes(rgb))
        if args.rotate:
            img = img.rotate(-args.rotate, expand=True)
        img.save(path)
        return path
    # 无 Pillow：退化成 PPM（无损，任何看图软件都能开）
    if args.rotate:
        print("[警告] 没有 Pillow，--rotate 被忽略")
    ppm = os.path.splitext(path)[0] + ".ppm"
    with open(ppm, "wb") as f:
        f.write(b"P6\n%d %d\n255\n" % (width, height))
        f.write(bytes(rgb))
    print("[提示] 未安装 Pillow，已输出 PPM：%s（可用画图打开另存 PNG）" % ppm)
    return ppm



# ============================================================================
# 4b. 自动侦测：不知道参数也能出图
# ============================================================================

# ST7735S 数据手册里会出现的命令（含各家初始化序列常用的扩展命令）
KNOWN_CMDS = set(CMD_NAMES) | {
    0xB1, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7, 0xB8, 0xB9,
    0xC0, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xE0, 0xE1,
    0x25, 0x26, 0x30, 0x38, 0x51, 0x53, 0xBB, 0xD0,
}


def measure_cs_active_low(samples) -> Tuple[bool, str]:
    """判断 CS 极性：时钟边沿一定发生在"有效"那一段时间里。

    片选无效时 SCL 是静止的，所以哪个 CS 电平上时钟边沿多，哪个就是有效电平。
    这比"谁空闲久"可靠 —— 连续刷屏时 CS 活动时间反而占多数。
    """
    toggles = {0: 0, 1: 0}
    prev_scl = None
    for (_t, scl, _sda, cs, _dc) in samples:
        if prev_scl is not None and scl != prev_scl:
            toggles[cs] += 1
        prev_scl = scl
    if toggles[0] == 0 and toggles[1] == 0:
        return True, "没看到时钟边沿，CS 极性按低有效猜"
    if toggles[0] >= toggles[1]:
        return True, ("时钟边沿 %d 个在 CS=0、%d 个在 CS=1 → CS 低有效"
                      % (toggles[0], toggles[1]))
    return False, ("时钟边沿 %d 个在 CS=0、%d 个在 CS=1 → CS 高有效"
                   % (toggles[0], toggles[1]))


def measure_scl_idle(samples, cs_active_low: bool) -> int:
    """CS 无效期间 SCL 停在哪 -> CPOL。"""
    act = 0 if cs_active_low else 1
    cnt = {0: 0, 1: 0}
    for (_t, scl, _sda, cs, _dc) in samples:
        if cs != act:
            cnt[scl] += 1
            if cnt[0] + cnt[1] > 200000:
                break
    return 0 if cnt[0] >= cnt[1] else 1


def score_run(lcd: "LcdParser", dec: "SpiDecoder") -> Tuple[float, str]:
    """给一次解码打分：命令可识别率 + 有没有 RAMWR/像素/窗口 + 合理性惩罚。"""
    if dec.n_bytes < 4:
        return -1e6, "没有解出字节"
    known = sum(n for v, n in lcd.cmd_hist.items() if v in KNOWN_CMDS)
    tot = max(1, lcd.n_cmd_bytes)
    ratio = known / tot
    s = 60.0 * ratio
    notes = ["命令可识别 %d%%" % round(ratio * 100)]

    if lcd.n_ramwr:
        s += 25
        notes.append("有 0x2C")
    if lcd.n_pixel_bytes:
        s += 20
        notes.append("有像素 %d" % (lcd.n_pixel_bytes // 2))
    if lcd.window_from_cmd:
        s += 10
        notes.append("有窗口命令")
    if lcd.frames:
        fill = max((f.pix / f.expected) for f in lcd.frames if f.expected)
        s += 15.0 * fill
        notes.append("填充 %.0f%%" % (fill * 100))
    if lcd.n_cmd_bytes > 0.5 * dec.n_bytes:
        s -= 60
        notes.append("命令占比过高")
    if lcd.n_data_bytes == 0:
        s -= 30
    return s, "，".join(notes)


def derive_offsets(lcd: "LcdParser", args) -> Tuple[int, int]:
    """从抓到的 0x2A/0x2B 窗口反推偏移。

    取"最大的那个窗口"（一次整屏刷新）来判断，不能取最后一帧——真实刷屏里
    绝大多数 RAMWR 是几个像素的局部修补。判据：
      窗口 x 跨度 == 面板宽 且 y 跨度 == 面板高  → x=0x2A、y=0x2B，起点即偏移
      窗口 x 跨度 == 面板高 且 y 跨度 == 面板宽  → 逻辑方向与面板差 90 度
    若 MADCTL 声明了 MV（行列交换）却仍然是前一种情况，说明 MV 对逻辑地址没生效
    （应用写的就是逻辑帧，控制器只管扫描方向），此时关掉几何变换，否则会多转 90 度。
    """
    if not lcd.frames:
        return args.offset_x, args.offset_y
    best = None
    for fr in lcd.frames:
        if fr.expected > 0 and (best is None or fr.expected > best.expected):
            best = fr
    if best is None:
        return args.offset_x, args.offset_y
    w = best.xe - best.xs + 1
    h = best.ye - best.ys + 1
    if w == args.width and h == args.height:
        if lcd._mapped_madctl() & 0x20:
            args.no_geom = True
        return best.xs, best.ys
    if w == args.height and h == args.width:
        return best.ys, best.xs
    return args.offset_x, args.offset_y


def auto_detect(samples, args, log: List[str]):
    """在候选组合里挑最合理的一组 SPI 时序参数。"""
    cs_low, cs_note = measure_cs_active_low(samples)
    log.append(cs_note)

    # CPOL/CPHA 的 4 种组合其实只有两个"采样沿"，所以只搜 2 x 2 x 2 = 8 组
    results = []
    for edge_rising in (True, False):
        for msb in (True, False):
            for dc_low in (True, False):
                cpol, cpha = (0, 0) if edge_rising else (0, 1)
                prm = SpiParams(cpol=cpol, cpha=cpha, msb_first=msb,
                                cs_active_low=cs_low, dc_command_low=dc_low)
                lcd = LcdParser(args)
                dec = SpiDecoder(prm, lcd.on_byte)
                dec.feed(samples)
                lcd.close()
                sc, why = score_run(lcd, dec)
                results.append((sc, prm, lcd, dec, why))
    # 平分时按"解出多少字节"决胜（错的 DC 极性一个字节都解不出来）
    results.sort(key=lambda r: (-r[0], -r[3].n_bytes))

    log.append("候选评分（前 4）：")
    for sc, prm, lcd, dec, why in results[:4]:
        log.append("  %7.1f  %s  %-3s  DC%s=命令   %s"
                   % (sc,
                      "上升沿" if prm.sample_on_rising else "下降沿",
                      "MSB" if prm.msb_first else "LSB",
                      "低" if prm.dc_command_low else "高",
                      why))

    sc, prm, lcd, dec, why = results[0]
    if sc < 0:
        log.append("! 所有组合都解不出东西，先检查接线/采样率")
        return prm, args.offset_x, args.offset_y, False

    # 判定是否"够可信"：有 RAMWR、分数为正、且领先第二名一截
    second = results[1][0] if len(results) > 1 else -1e9
    decisive = (sc > 0 and lcd.n_ramwr > 0 and (sc - second) > 5)

    # 补上 CPOL：只看采样沿就够了，但把真实 CPOL 报出来免得你看日志犯迷糊
    cpol = measure_scl_idle(samples, cs_low)
    if prm.sample_on_rising:
        cpha = 1 if cpol == 1 else 0
    else:
        cpha = 0 if cpol == 1 else 1
    prm.cpol, prm.cpha = cpol, cpha

    ox, oy = derive_offsets(lcd, args)
    log.append("胜出：mode%d + %s + DC%s=命令，窗口 x=%d..%d y=%d..%d → offset=(%d,%d)"
               % (0 if (prm.cpol, prm.cpha) == (0, 0) else
                  1 if (prm.cpol, prm.cpha) == (0, 1) else
                  2 if (prm.cpol, prm.cpha) == (1, 0) else 3,
                  "MSB first" if prm.msb_first else "LSB first",
                  "低" if prm.dc_command_low else "高",
                  lcd.xs, lcd.xe, lcd.ys, lcd.ye, ox, oy))
    return prm, ox, oy, decisive


def byte_order_by_smoothness(frame: "Frame", args) -> Tuple[str, float, float]:
    """比对两种字节序拼出来的图，谁更"平滑"谁对（错序会把亮度噪声搬进色度）。"""
    scores = {}
    for name, conv in (("big", lambda v: v),
                       ("little", lambda v: ((v & 0xFF) << 8) | (v >> 8))):
        f = Frame(frame.xs, frame.xe, frame.ys, frame.ye,
                  args.width, args.height, frame.ox, frame.oy,
                  frame.madctl, frame.transform, frame.bgr, frame.index)
        for v in frame.vals:
            f.put(conv(v), 0)
        tot = 0
        cnt = 0
        w = args.width
        for y in range(args.height):
            base = y * w
            for x in range(1, w):
                i = base + x
                j = i - 1
                if f.cover[i] and f.cover[j]:
                    tot += (abs(f.canvas[3 * i] - f.canvas[3 * j])
                            + abs(f.canvas[3 * i + 1] - f.canvas[3 * j + 1])
                            + abs(f.canvas[3 * i + 2] - f.canvas[3 * j + 2]))
                    cnt += 1
        scores[name] = (tot / cnt) if cnt else 1e9
    best = "big" if scores["big"] <= scores["little"] else "little"
    return best, scores["big"], scores["little"]


# ============================================================================
# 5. 命令行
# ============================================================================

def parse_rate(s: str) -> float:
    t = s.strip().lower().replace("hz", "")
    mult = 1.0
    for suf, m in (("g", 1e9), ("m", 1e6), ("k", 1e3)):
        if t.endswith(suf):
            t = t[:-1]
            mult = m
            break
    return float(t) * mult


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="从逻辑分析仪 SPI 抓包还原 ST7735S 屏幕截图",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("input", help="输入文件（.csv / .vcd）")
    ap.add_argument("-o", "--out", default="st7735_shot.png", help="输出 PNG 路径")

    # ---- 采样与电气 ----
    g = ap.add_argument_group("采样 / 电平")
    g.add_argument("--sample-rate", type=parse_rate, default=24e6,
                   help="逻辑分析仪采样率，Hz（默认 24e6）。注意 >= 4 x SPI 时钟")
    g.add_argument("--format", choices=("auto", "csv", "vcd"), default="auto", help="输入格式")
    g.add_argument("--encoding", default="utf-8", help="文本编码，Windows 上可能是 gbk")
    g.add_argument("--header", choices=("auto", "yes", "no"), default="auto",
                   help="是否含表头（默认自动判断）")

    # ---- 列映射（改这里就能适配别的导出格式）----
    g = ap.add_argument_group("列映射（名字或 0 起的列号）")
    g.add_argument("--columns", default=None,
                   help='按位置一次给全：time,scl,sda,cs,dc，例如 0,1,2,3,4')
    g.add_argument("--time-col", default=None, dest="time_col")
    g.add_argument("--scl-col", default=None, dest="scl_col")
    g.add_argument("--sda-col", default=None, dest="sda_col")
    g.add_argument("--cs-col", default=None, dest="cs_col")
    g.add_argument("--dc-col", default=None, dest="dc_col")

    # ---- SPI 协议 ----
    g = ap.add_argument_group("SPI 协议")
    g.add_argument("--spi-mode", type=int, choices=(0, 1, 2, 3), default=0,
                   help="0=CPOL0/CPHA0（默认）")
    g.add_argument("--cpol", type=int, choices=(0, 1), default=None, help="覆盖 mode 的 CPOL")
    g.add_argument("--cpha", type=int, choices=(0, 1), default=None, help="覆盖 mode 的 CPHA")
    g.add_argument("--bit-order", choices=("msb", "lsb"), default="msb")
    g.add_argument("--cs-active", choices=("low", "high"), default="low", help="片选有效电平")
    g.add_argument("--dc-command", choices=("low", "high"), default="low",
                   help="DC 为命令时的电平（默认 low）")

    # ---- 面板 / 图像 ----
    g = ap.add_argument_group("面板 / 图像")
    g.add_argument("--width", type=int, default=160)
    g.add_argument("--height", type=int, default=128)
    g.add_argument("--offset-x", type=int, default=0, dest="offset_x",
                   help="控制器列偏移（132 列 GRAM 常见 0/1/2）")
    g.add_argument("--offset-y", type=int, default=0, dest="offset_y",
                   help="控制器行偏移（162 行 GRAM 常见 0/1/24/32）")
    g.add_argument("--byte-order", choices=("auto", "big", "little"), default="auto",
                   help="RGB565 高低字节顺序；auto 用图像平滑度自检，默认 auto")
    g.add_argument("--madctl", default="auto", help="auto | none(不做几何变换) | 0xNN")
    g.add_argument("--color-order", choices=("auto", "rgb", "bgr"), default="auto",
                   help="auto 时按 MADCTL bit3 判断")
    g.add_argument("--render", choices=("composite", "last", "first", "all"), default="composite",
                   help="多帧时的输出策略（默认叠加所有帧）")
    g.add_argument("--bg", default="000000", help="未写入区域的背景色 RRGGBB")
    g.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0,
                   help="输出图顺时针旋转（需要 Pillow）")
    g.add_argument("--auto", action="store_true",
                   help="自动侦测 SPI 模式/位序/DC 极性/偏移/字节序（不知道参数就加这个）")
    g.add_argument("--auto-probe", type=int, default=400000, dest="auto_probe",
                   help="自动侦测时先取样多少个采样点（默认 40 万）")
    g.add_argument("-v", "--verbose", action="store_true")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Windows 控制台默认 cp936，打印中文/符号可能炸
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    ap = build_argparser()
    args = ap.parse_args(argv)

    if not os.path.isfile(args.input):
        print("错误：找不到文件 %s" % args.input)
        return 2
    if not HAVE_PIL:
        print("[提示] 未检测到 Pillow（pip install pillow），将输出 PPM")

    argv_list = list(sys.argv[1:] if argv is None else argv)
    want_auto_order = (args.byte_order == "auto")
    if want_auto_order:
        args.byte_order = "big"      # 先按大端解，后面用平滑度自检再反转
    x_given = any(a.startswith("--offset-x") for a in argv_list)
    y_given = any(a.startswith("--offset-y") for a in argv_list)

    # ---- SPI 参数 ----
    cpol = args.cpol if args.cpol is not None else (args.spi_mode & 2) >> 1
    cpha = args.cpha if args.cpha is not None else (args.spi_mode & 1)
    params = SpiParams(
        cpol=cpol, cpha=cpha,
        msb_first=(args.bit_order != "lsb"),
        cs_active_low=(args.cs_active == "low"),
        dc_command_low=(args.dc_command == "low"),
    )

    auto_log: List[str] = []

    def run_full():
        _lcd = LcdParser(args)
        _dec = SpiDecoder(params, _lcd.on_byte)
        _src, _kind = open_sample_source(args.input, args)
        _dec.feed(_src)
        _lcd.close()
        return _lcd, _dec, _kind

    # ---- 自动侦测 ----
    if args.auto:
        probe_n = max(1000, args.auto_probe)
        while True:
            it, _k = open_sample_source(args.input, args)
            probe = list(islice(it, probe_n))
            trial_log: List[str] = []
            params, ox, oy, ok = auto_detect(probe, args, trial_log)
            if ok or len(probe) < probe_n or probe_n >= 8_000_000:
                auto_log = trial_log
                auto_log.insert(0, "取样 %d 个点做候选搜索%s"
                                % (len(probe), "" if ok else "（数据不够，结果仅供参考）"))
                break
            probe_n *= 4          # 判不出胜负就把探测窗口放大 4 倍再来
        if not x_given:
            args.offset_x = ox
        if not y_given:
            args.offset_y = oy
        cpol, cpha = params.cpol, params.cpha
        args.spi_mode = 0 if (cpol, cpha) == (0, 0) else \
                        1 if (cpol, cpha) == (0, 1) else \
                        2 if (cpol, cpha) == (1, 0) else 3
        args.bit_order = "msb" if params.msb_first else "lsb"
        args.cs_active = "low" if params.cs_active_low else "high"
        args.dc_command = "low" if params.dc_command_low else "high"

    lcd, dec, src_kind = run_full()

    # 全量跑完再用真实窗口校正一次偏移（侦测时可能还没抓到 0x2A/0x2B）
    if args.auto and lcd.frames and (not x_given or not y_given):
        prev_ng = getattr(args, "no_geom", False)
        ox, oy = derive_offsets(lcd, args)
        n_ox = args.offset_x if x_given else ox
        n_oy = args.offset_y if y_given else oy
        ng_changed = getattr(args, "no_geom", False) != prev_ng
        if (n_ox, n_oy) != (args.offset_x, args.offset_y) or ng_changed:
            if (n_ox, n_oy) != (args.offset_x, args.offset_y):
                auto_log.append("按实际窗口修正偏移 (%d,%d) -> (%d,%d)"
                                % (args.offset_x, args.offset_y, n_ox, n_oy))
            if ng_changed:
                auto_log.append("MADCTL 声明了 MV，但整屏窗口 x 跨度 = 面板宽 → MV 对"
                                "逻辑地址未生效，已关闭几何变换（否则图会多转 90°）")
            args.offset_x, args.offset_y = n_ox, n_oy
            lcd, dec, src_kind = run_full()

    # ---- 字节序自检 ----
    if want_auto_order and lcd.frames:
        # 用"最大且不是纯色"的一帧做自检：真实刷屏里常有整屏清黑的一帧，
        # 那种帧两种字节序算出来都是 0，自检等于没做。
        cands = [f for f in lcd.frames if f.expected > 0]
        rich = [f for f in cands if len(set(f.vals)) >= 4]
        pool = rich or cands
        big = max(pool, key=lambda f: f.expected) if pool else None
        best, sb, sl = (byte_order_by_smoothness(big, args) if big
                        else ("big", 0.0, 0.0))
        auto_log.append("字节序自检：高字节先平滑度 %.1f / 低字节先 %.1f → 取%s"
                        % (sb, sl, "高字节先" if best == "big" else "低字节先"))
        args.byte_order = best
        if best == "little":
            lcd, dec, src_kind = run_full()

    # ---- SPI 时钟估算 ----
    spb = dec.median_edge_spacing()
    edge_dt = dec.median_edge_dt()
    if src_kind in ("vcd", "pv"):
        # VCD / 边沿表记录的是精确边沿，没有"采样点"概念，直接用时间戳算周期
        f_spi = (1.0 / edge_dt) if edge_dt > 0 else 0.0
        spb = 0.0
    else:
        f_spi = (args.sample_rate / spb) if spb > 0 else 0.0

    # ---- 选帧 ----
    frames = lcd.frames
    outs: List[Tuple[str, List[Frame]]] = []
    if not frames:
        outs.append((args.out, []))
    elif args.render == "all":
        base, ext = os.path.splitext(args.out)
        ext = ext or ".png"
        for i, f in enumerate(frames):
            outs.append(("%s_%03d%s" % (base, i, ext), [f]))
    elif args.render == "last":
        outs.append((args.out, [frames[-1]]))
    elif args.render == "first":
        outs.append((args.out, [frames[0]]))
    else:
        outs.append((args.out, list(frames)))

    written: List[str] = []
    for path, sel in outs:
        rgb = composite(sel, args.width, args.height, parse_color(args.bg))
        written.append(save_png(path, args.width, args.height, rgb, args))

    # ---- 统计 ----
    total_tx_used = sum(len(f.txs) for f in frames)
    spanning = [f for f in frames if len(f.txs) > 1]
    extra_tx = sum(len(f.txs) - 1 for f in frames)
    clipped = sum(f.clipped for f in frames)
    overflow = sum(f.overflow for f in frames)

    print()
    if auto_log:
        print("-" * 68)
        print("自动侦测")
        for line in auto_log:
            print("  " + line)
    if getattr(args, "_no_csdc", False):
        print("!" * 68)
        print("接线体检：这个抓包无法还原屏幕")
        print("  · CS 和 DC 两路从头到尾没有任何电平变化（只有 SCL / MOSI 在动）")
        print("  · 没有 DC 就分不出命令字节和像素字节，也就找不到 0x2C")
        print("  · 需要补抓 CS（片选）和 DC（命令/数据）两根线，见 README 第 1 节")
        print("!" * 68)
    if getattr(args, "_not_a_bus", False):
        conc = getattr(args, "_bus_conc", 0.0) or 0.0
        print("!" * 68)
        print("接线体检：这堆线上根本没有 SPI 总线")
        print("  · 最好的“时钟+数据”配对，相位集中度只有 %.0f%%（真总线 >75%%）" % (conc * 100))
        print("  · 同一颗 MCU 驱动的 SCL 和 MOSI，一定在时钟周期的同一相位翻转；")
        print("    实测各通道边沿互不对齐 ⇒ 夹子里没有一根是 SDA 或 SCL")
        print("  · 上面“通道体检”表里写的就是它们实际是什么（背光 PWM / 开关电源等）")
        print()
        print("  这块板子的 LCD 是软件位翻转驱动，引脚是固定的（固件实测）：")
        print("      SCL = PA6    SDA = PA7    CS = PC2(低有效)")
        print("      DC  = PC4(高=数据/低=命令)    RESX = PC5")
        print("  对应排线针脚：9 SCL / 8 SDA / 7 DCX / 12 CSX / 6 RESX")
        print("  因为是软件位翻转，时钟约 590kHz、占空比不对称、周期有抖动，这是正常的。")
        print()
        print("  ⚠ 连接器 AFC11-S14ICC-00 是 0.5mm 间距，14 针总宽只有 6.5mm。")
        print("    测试钩约 2mm 宽，必然压住 3~4 针 ⇒ 相邻的 SDA(8) 和 SCL(9) 会被短路。")
        print("    改法：① 夹 MCU 引脚（TQFP64 0.8mm，引脚在外缘，细针可单点接触）")
        print("          ② 焊 0.1mm 漆包线到焊盘  ③ 用 0.5mm FPC 测试转接板")
        print()
        print("  抓之前先制造整屏刷新——断电重上电、切页面、按菜单，然后抓 ≥5 秒")
        print("  （画面静止时总线上几乎没有数据，必定抓不到 0x2A/0x2B/0x2C）")
        print("!" * 68)
    print("=" * 68)
    print("ST7735S 解析结果")
    print("=" * 68)
    print("输入文件        : %s" % args.input)
    print("采样点数        : %d" % dec.n_samples)
    print("CS 事务数       : %d" % dec.tx_count)
    print("SPI 字节数      : %d（命令 %d / 数据 %d）" % (dec.n_bytes, dec.n_cmd, dec.n_data))
    print("命令数(DC=0)    : %d" % lcd.n_cmd_bytes)
    print("RAMWR(0x2C)次数 : %d" % lcd.n_ramwr)
    print("像素字节数      : %d  =>  %d 个像素" % (lcd.n_pixel_bytes, lcd.n_pixel_bytes // 2))
    print("跨 CS 事务      : %d 帧被拆到多个事务，共多用 %d 个事务（涉及 %d 个）"
          % (len(spanning), extra_tx, total_tx_used))
    print("帧数            : %d（输出策略 %s）" % (len(frames), args.render))
    print("SPI 参数        : mode%s (CPOL=%d CPHA=%d), %s first, 采样沿=%s, CS %s有效, DC %s=命令"
          % (args.spi_mode, cpol, cpha, args.bit_order.upper(),
             "上升" if params.sample_on_rising else "下降",
             "低" if params.cs_active_low else "高",
             "低" if params.dc_command_low else "高"))
    if f_spi > 0 and src_kind in ("vcd", "pv"):
        print("SPI 时钟(实测)  : %.3f MHz（由边沿时间戳算出，与采样率无关）" % (f_spi / 1e6))
    elif spb > 0:
        print("SPI 时钟(实测)  : %.3f MHz（每位 %.2f 个采样点，采样率 %.2f MHz）"
              % (f_spi / 1e6, spb, args.sample_rate / 1e6))
    else:
        print("SPI 时钟(实测)  : 无法估算（没抓到时钟边沿）")
    print("MADCTL          : %s" % (madctl_bits(lcd.madctl) if lcd.madctl_seen else "未见 0x36"))
    print("颜色顺序        : %s" % ("BGR(已交换 R/B)" if lcd.decide_bgr() else "RGB"))
    print("面板 / 偏移     : %dx%d, offset=(%d,%d), 字节序=%s"
          % (args.width, args.height, args.offset_x, args.offset_y,
             "高字节先" if args.byte_order == "big" else "低字节先"))
    for f in frames[-4:]:
        print("  帧#%-3d 窗口 x=%d..%d y=%d..%d 已写 %d/%d 像素 (%.1f%%) 事务=%d%s"
              % (f.index, f.xs, f.xe, f.ys, f.ye, f.pix, f.expected,
                 100.0 * f.pix / f.expected if f.expected else 0.0,
                 len(f.txs), "" if f.complete else "  [不完整]"))

    if args.verbose and lcd.cmd_hist:
        print("-" * 68)
        print("命令直方图:")
        for v, n in sorted(lcd.cmd_hist.items(), key=lambda kv: -kv[1])[:20]:
            print("  0x%02X %-16s x%d" % (v, CMD_NAMES.get(v, "?"), n))
        print("关键命令序列（前 40 条）:")
        for line in lcd.key_log[:40]:
            print("  " + line)

    # ---- 警告 ----
    warns: List[str] = []
    if f_spi > 6.2e6:
        warns.append("SPI 时钟约 %.2f MHz > 6MHz：请**临时降频**到 6MHz 以下再抓"
                     "（24MHz 采样要求 SPI <= 6MHz，否则边沿可能被漏采、字节错位）。"
                     "如果现在这张图看着是乱码，先降频重抓。" % (f_spi / 1e6))
    if src_kind == "csv" and spb and args.sample_rate < 4 * f_spi * 0.95:
        warns.append("采样率只有 SPI 时钟的 %.1f 倍（要求 >= 4 倍），可靠性不足"
                     % (args.sample_rate / f_spi))
    elif src_kind == "csv" and spb and spb < 4:
        warns.append("每位只有 %.2f 个采样点（要求 >= 4），建议提高采样率或降低 SPI 时钟" % spb)
    if frames:
        inc = [f for f in frames if not f.complete]
        if inc:
            warns.append("%d 帧像素不足（抓包结束/被新命令打断），已按已解析部分输出"
                         % len(inc))
        full = [f for f in frames if f.expected > 0 and f.pix >= f.expected]
        if not full and len(frames) == 1:
            warns.append("这一帧没有写满窗口（%.1f%%），可能是部分刷新或抓包不完整"
                         % (100.0 * frames[0].pix / max(1, frames[0].expected)))
    if clipped:
        warns.append("有 %d 个像素落在图像范围外被裁掉：offset/宽高/MADCTL 很可能不对"
                     "（试着调 --offset-x/--offset-y，或 --madctl none）" % clipped)
    if overflow:
        warns.append("窗口写满后还收到 %d 个像素（控制器回绕），已忽略" % overflow)
    if not lcd.window_from_cmd:
        warns.append("没有抓到 0x2A/0x2B，窗口按 offset+宽高 推测，可能出现错位")
    warns += dec.warnings + lcd.warnings

    print("-" * 68)
    if warns:
        print("警告 (%d):" % len(warns))
        for w in warns:
            print("  [!] " + w)
    else:
        print("警告: 无")
    print("-" * 68)
    for p in written:
        print("输出: %s" % os.path.abspath(p))
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
