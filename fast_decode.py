"""直接从 Logic 2 导出的「跳变表 CSV」解码出所有页面（快速路径）。

旧路径的慢点：
  1. csv 模块逐行解析 2200 万行      → 改用 pandas C 引擎
  2. csv2bins 把每个采样点展开成位数组 → 完全不需要：CSV 每行本身就是「该时刻的电平」，
     所以在 SCL 上升沿那一行直接读 SDA/DC/CS 的电平即可，零解包。

用法：
  python fast_decode.py digital.csv --out-dir pages_fast
  python fast_decode.py digital.csv --stats-only                    # 只看通道接没接对
  python fast_decode.py digital.csv --channels 0 1 2 3              # 指定列（默认值）
"""
import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import st7735_decode as D

LCD_W, LCD_H = 160, 128
OFF_X, OFF_Y = 1, 2

# 2026-11-02 实测定案：Ch0=SCL Ch1=CS Ch2=DC Ch3=SDA
CH_SCL, CH_CS, CH_DC, CH_SDA = 0, 1, 2, 3


def read_csv(path, cols=(CH_SCL, CH_CS, CH_DC, CH_SDA)):
    """读跳变表 → (时间, SCL, CS, DC, SDA)。

    cols=(scl, cs, dc, sda) 是「数据列序号」，和逻辑分析仪界面上的 Channel N 一致
    （Channel 0 = 第 1 个数据列，CSV 里对应第 2 列）。默认 (0,1,2,3)。
    """
    want = [0] + [c + 1 for c in cols]
    try:
        import pandas as pd
        # 先取表头：pandas 的 usecols 返回的是「文件中的列顺序」，不是传入顺序，
        # 所以按列名取完还要显式重排一次。
        names = pd.read_csv(path, nrows=0).columns.tolist()
        if max(want) >= len(names):
            raise SystemExit(
                "CSV 只有 %d 列，要读第 %d 列。\n"
                "  用 --channels SCL CS DC SDA 指定正确的列号（Channel N 的 N）。"
                % (len(names), max(want)))
        pick = [names[i] for i in want]
        df = pd.read_csv(
            path, engine="c", header=0, usecols=pick,
            dtype={pick[0]: np.float64, **{n: np.int8 for n in pick[1:]}},
        )
        a = df[pick].to_numpy()
        return a[:, 0], a[:, 1].copy(), a[:, 2].copy(), a[:, 3].copy(), a[:, 4].copy()
    except ImportError:
        print("  [i] 没装 pandas，回退到内置慢速解析（会慢 3~5 倍）", flush=True)
    except ValueError as e:
        raise SystemExit("读列失败：%s\n"
                         "  CSV 的列不够？先用 diag_channels.py 看有几路信号，"
                         "再用 --channels SCL CS DC SDA 指定列号。" % e)

    out = [[] for _ in range(5)]
    with open(path, "rb") as f:
        f.readline()
        for line in f:
            p = line.rstrip(b"\r\n").split(b",")
            if len(p) <= max(want):
                continue
            out[0].append(float(p[0]))
            for k in range(4):
                out[k + 1].append(int(p[want[k + 1]]))
    return (np.array(out[0], np.float64), np.array(out[1], np.int8),
            np.array(out[2], np.int8), np.array(out[3], np.int8),
            np.array(out[4], np.int8))


def build_pairs(t, scl, cs, dc, sda, rising=True):
    """SCL 上升沿取电平 → 按 CS 事务分组 → 每 8 沿一个字节（MSB 先）。"""
    if rising:
        edge = np.flatnonzero((scl[1:] == 1) & (scl[:-1] == 0)) + 1
    else:
        edge = np.flatnonzero((scl[1:] == 0) & (scl[:-1] == 1)) + 1

    act = cs[edge] == 0                      # CS 低有效
    idx = np.flatnonzero(act)
    sel = act[idx]
    chg = np.empty(len(sel), bool)
    chg[0] = True
    chg[1:] = sel[1:] != sel[:-1]
    run = np.cumsum(chg) - 1
    starts = np.flatnonzero(np.r_[True, run[1:] != run[:-1]])
    counts = np.diff(np.r_[starts, len(sel)])
    pos = np.arange(len(sel)) - np.repeat(starts, counts)

    grp = pos // 8
    bp = 7 - (pos % 8)
    key = run.astype(np.int64) * (1 << 20) + grp
    u, inv = np.unique(key, return_inverse=True)

    # 注意：idx 是「时钟沿数组」的下标，必须先按沿采样再取子集
    sd_e = sda[edge]
    dc_e = dc[edge]
    sd = sd_e[idx].astype(np.uint16)
    val = np.zeros(len(u), np.uint16)
    np.add.at(val, inv, sd << bp.astype(np.uint16))
    full = np.bincount(inv, minlength=len(u))
    last = bp == 0
    dcb = np.zeros(len(u), np.uint8)
    dcb[inv[last]] = dc_e[idx][last]
    order = inv[last]
    keep = order[full[order] >= 8]
    return val[keep].astype(np.uint8), (dcb[keep] == 1).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--out-dir", default="pages_fast")
    ap.add_argument("--stride", type=int, default=20)
    ap.add_argument("--plateau", type=float, default=0.10)
    ap.add_argument("--dedup", type=float, default=0.6)
    ap.add_argument("--min-run", type=int, default=8)
    ap.add_argument("--stats-only", action="store_true")
    ap.add_argument("--channels", type=int, nargs=4, default=[0, 1, 2, 3],
                    metavar=("SCL", "CS", "DC", "SDA"),
                    help="CSV 里的数据列序号（Channel N 的 N），默认 0 1 2 3")
    ap.add_argument("--width", type=int, default=LCD_W)
    ap.add_argument("--height", type=int, default=LCD_H)
    ap.add_argument("--offset-x", type=int, default=OFF_X,
                    help="GRAM 列偏移，默认 %d（这块屏的实测值）" % OFF_X)
    ap.add_argument("--offset-y", type=int, default=OFF_Y,
                    help="GRAM 行偏移，默认 %d（这块屏的实测值）" % OFF_Y)
    ap.add_argument("--madctl", default="none", choices=["none", "auto"])
    ap.add_argument("--byte-order", default="big", choices=["big", "little"])
    ap.add_argument("--color-order", default="rgb", choices=["rgb", "bgr", "auto"])
    ap.add_argument("--rising", action="store_true", help="强制用上升沿")
    ap.add_argument("--falling", action="store_true", help="强制用下降沿")
    args = ap.parse_args()

    T = time.time()
    t, scl, cs, dc, sda = read_csv(args.csv, tuple(args.channels))
    print("[%.1fs] 读表完成 %d 行  %.3f s" % (time.time() - T, len(t), t[-1]), flush=True)
    print("  翻转: SCL=%d CS=%d DC=%d SDA=%d"
          % ((np.diff(scl) != 0).sum(), (np.diff(cs) != 0).sum(),
             (np.diff(dc) != 0).sum(), (np.diff(sda) != 0).sum()), flush=True)

    if args.rising:
        cands = [True]
    elif args.falling:
        cands = [False]
    else:
        cands = [True, False]

    best = None
    for rising in cands:
        v, d = build_pairs(t, scl, cs, dc, sda, rising=rising)
        cmds = v[d == 0]
        cnt = np.bincount(cmds, minlength=256)
        nA = int(cnt[0x2A] + cnt[0x2B] + cnt[0x2C])
        uniq = int((cnt > 0).sum())
        if len(cands) > 1:
            print("    %s沿: 字节 %d 命令种类 %d 2A/2B/2C=%d"
                  % ("上升" if rising else "下降", len(v), uniq, nA), flush=True)
        score = (nA, -uniq)
        if best is None or score > best[0]:
            best = (score, rising, v, d)
    (nA, _neg), rising, val, isdata = best
    print("[%.1fs] 采用%s沿  字节流 %d"
          % (time.time() - T, "上升" if rising else "下降", len(val)), flush=True)
    if nA < 1000:
        print("  ⚠ 2A/2B/2C 只命中 %d 次 —— 通道角色可能不对。\n"
              "    先跑：python diag_channels.py %s" % (nA, args.csv), flush=True)

    pargs = D.build_argparser().parse_args([
        os.devnull, "-o", os.devnull,
        "--width", str(args.width), "--height", str(args.height),
        "--offset-x", str(args.offset_x), "--offset-y", str(args.offset_y),
        "--madctl", args.madctl, "--byte-order", args.byte_order,
        "--color-order", args.color_order,
    ])
    lcd = D.LcdParser(pargs)
    on = lcd.on_byte
    for v, d in zip(val.tolist(), isdata.tolist()):
        on(v, d, 1)
    lcd.close()
    print("[%.1fs] 帧 %d  命令种类 %d" % (time.time() - T, len(lcd.frames), len(lcd.cmd_hist)), flush=True)
    top = sorted(lcd.cmd_hist.items(), key=lambda x: -x[1])[:6]
    print("  命令 top:", [(hex(k), v) for k, v in top], flush=True)
    if args.stats_only:
        return

    import snapshot_pages as SP
    frames = lcd.frames
    canvas = np.zeros((args.height, args.width, 3), dtype=np.uint8)
    snaps = []
    st = args.stride
    for i, f in enumerate(frames):
        cov = np.frombuffer(bytes(f.cover), dtype=np.uint8).reshape(args.height, args.width) > 0
        if cov.any():
            src = np.frombuffer(bytes(f.canvas), dtype=np.uint8).reshape(args.height, args.width, 3)
            canvas[cov] = src[cov]
        if (i + 1) % st == 0:
            snaps.append((i + 1, canvas.copy()))
    if frames:
        snaps.append((len(frames), canvas.copy()))
    print("[%.1fs] 快照 %d" % (time.time() - T, len(snaps)), flush=True)
    if not snaps:
        print("  ✗ 解出 0 帧 —— 没找到完整的 0x2A/0x2B/0x2C 序列。\n"
              "    先跑 diag_channels.py 确认通道角色和采样沿。", flush=True)
        return

    runs, cur = [], [snaps[0]]
    for i in range(1, len(snaps)):
        if SP.diff_pct(snaps[i - 1][1], snaps[i][1]) < args.plateau:
            cur.append(snaps[i])
        else:
            runs.append(cur)
            cur = [snaps[i]]
    runs.append(cur)
    cands = [r[-1] for r in runs if len(r) >= args.min_run]
    if not cands:
        # 抓得很短（只有一帧）时没有「平台段」，直接把最后一张合成图画出来
        print("  [i] 没有连续静止的平台段，直接输出最后一张合成图", flush=True)
        cands = [snaps[-1]]
    kept = []
    for idx, img in cands:
        if kept and min(SP.diff_pct(k, img) for _, k in kept) < args.dedup:
            continue
        kept.append((idx, img))
    print("[%.1fs] 平台 %d → 候选 %d → 去重 %d 页"
          % (time.time() - T, len(runs), len(cands), len(kept)), flush=True)

    from PIL import Image
    os.makedirs(args.out_dir, exist_ok=True)
    for n, (idx, img) in enumerate(kept, 1):
        Image.fromarray(img).save(os.path.join(args.out_dir, "page_%02d.png" % n))
        print("  page_%02d  第 %d 帧" % (n, idx), flush=True)
    print("[%.1fs] 完成 → %s" % (time.time() - T, args.out_dir), flush=True)


if __name__ == "__main__":
    main()
