"""通道角色 / 采样相位自检：万一夹错线或时钟相位变了，用这个一次找出正确组合。

原理：ST7735S 的命令集很小（20 来个），而 0x2A(列地址) / 0x2B(行地址) / 0x2C(写显存)
在一帧里会各出现一次。所以「解出来的命令里 2A/2B/2C 占绝大多数、命令种类数很少」
就是正确组合；错了会解出 240 多种不存在的命令。

用法：
  python diag_channels.py digital.csv
  python diag_channels.py digital.csv --channels 0 1 2 3     # 只在这 4 路里排
"""
import argparse
import collections
import itertools
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fast_decode as F

# ST7735S 真实命令集（用来给"解对了"打分）
LCMD = {
    0x01, 0x10, 0x11, 0x13, 0x20, 0x21, 0x25, 0x26, 0x28, 0x29, 0x2A, 0x2B, 0x2C, 0x2D, 0x2E,
    0x30, 0x33, 0x36, 0x37, 0x38, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E,
    0xB1, 0xB2, 0xB3, 0xB4, 0xB7, 0xB8, 0xB9, 0xBA, 0xBB, 0xBC,
    0xC0, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7,
    0xD0, 0xD1, 0xD2, 0xDA, 0xDB, 0xDC, 0xDD,
    0xE0, 0xE1, 0xF6,
}


def score(scl, cs, dc, sda, rising, cs_low, msb, dc_last):
    """按给定假设解一遍字节流，返回打分与统计。"""
    if rising:
        edge = np.flatnonzero((scl[1:] == 1) & (scl[:-1] == 0)) + 1
    else:
        edge = np.flatnonzero((scl[1:] == 0) & (scl[:-1] == 1)) + 1
    if len(edge) < 200:
        return None

    act = (cs[edge] == 0) if cs_low else (cs[edge] == 1)
    idx = np.flatnonzero(act)
    if len(idx) < 200:
        return None
    sel = act[idx]
    chg = np.empty(len(sel), bool)
    chg[0] = True
    chg[1:] = sel[1:] != sel[:-1]
    run = np.cumsum(chg) - 1
    starts = np.flatnonzero(np.r_[True, run[1:] != run[:-1]])
    counts = np.diff(np.r_[starts, len(sel)])
    pos = np.arange(len(sel)) - np.repeat(starts, counts)

    grp = pos // 8
    bp = (7 - (pos % 8)) if msb else (pos % 8)
    key = run.astype(np.int64) * (1 << 20) + grp
    u, inv = np.unique(key, return_inverse=True)

    se = sda[edge][idx].astype(np.uint16)
    de = dc[edge][idx]
    val = np.zeros(len(u), np.uint16)
    np.add.at(val, inv, se << bp.astype(np.uint16))
    full = np.bincount(inv, minlength=len(u))

    if (msb and dc_last) or (not msb and not dc_last):
        pick = bp == 0
    else:
        pick = bp == 7
    dcb = np.zeros(len(u), np.uint8)
    dcb[inv[pick]] = de[pick]
    order = inv[pick]
    keep = order[full[order] >= 8]

    v = val[keep]
    d = dcb[keep]
    cnt = collections.Counter(v[d == 0].tolist())
    nA = sum(cnt.get(x, 0) for x in (0x2A, 0x2B, 0x2C))
    return {
        "bytes": len(keep),
        "ncmd": len(cnt),
        "hit": len(set(cnt) & LCMD),
        "nA": nA,
        "top": cnt.most_common(4),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--channels", type=int, nargs=4, default=[0, 1, 2, 3],
                    metavar=("SCL", "CS", "DC", "SDA"))
    args = ap.parse_args()

    # 固定按 (SCL, CS, DC, SDA) 的顺序读，内部再换位角色
    t, a, b, c, d = F.read_csv(args.csv)
    raw = [a, b, c, d]
    name = dict(zip(args.channels, raw))
    scl = name[args.channels[0]]
    pool = {args.channels[1]: raw[1], args.channels[2]: raw[2], args.channels[3]: raw[3]}

    print("行数 %d  时长 %.3f s" % (len(t), t[-1]))
    for ch, arr in zip(args.channels, raw):
        print("  ch%d 翻转=%d 高电平占比=%.3f"
              % (ch, (np.diff(arr) != 0).sum(), arr.mean()))

    res = []
    for cs_c, dc_c, sda_c in itertools.permutations(pool):
        for rising in (True, False):
            for cs_low in (True, False):
                for msb in (True, False):
                    for dc_last in (True, False):
                        r = score(scl, pool[cs_c], pool[dc_c], pool[sda_c],
                                  rising, cs_low, msb, dc_last)
                        if r:
                            res.append(((r["nA"], -r["ncmd"]), cs_c, dc_c, sda_c,
                                        rising, cs_low, msb, dc_last, r))
    if not res:
        print("没有任何组合能解出字节流 —— 检查 SCL 那一路是否真的是时钟")
        return
    res.sort(key=lambda x: x[0], reverse=True)

    print("\n%8s %5s %5s | %-5s %-5s %-5s %-6s %-6s %-5s %-6s | %8s %5s"
          % ("2A/2B/2C", "命中", "种类", "CS", "DC", "SDA", "采样沿", "CS有效",
             "位序", "DC采样", "字节数", "命令数"))
    for _, cs_c, dc_c, sda_c, rising, cs_low, msb, dc_last, r in res[:6]:
        print("%8d %5d %5d | ch%-3d ch%-3d ch%-3d %-6s %-6s %-5s %-6s | %8d %5d   %s"
              % (r["nA"], r["hit"], r["ncmd"], cs_c, dc_c, sda_c,
                 "上升沿" if rising else "下降沿", "低" if cs_low else "高",
                 "MSB" if msb else "LSB", "末位" if dc_last else "首位",
                 r["bytes"], r["ncmd"], [(hex(k), v) for k, v in r["top"]]))

    best = res[0]
    _, cs_c, dc_c, sda_c, rising, cs_low, msb, dc_last, r = best
    ok = r["nA"] > 1000 and r["ncmd"] < 60
    print("\n推荐：CS=ch%d  DC=ch%d  SDA=ch%d  %s%s"
          % (cs_c, dc_c, sda_c, "上升沿" if rising else "下降沿",
             "" if cs_low else "（CS 高有效）"))
    if ok:
        print("     2A/2B/2C 共 %d 次、命令只有 %d 种 —— 这个组合是对的。"
              % (r["nA"], r["ncmd"]))
        print("     直接跑：python fast_decode.py <csv> --out-dir pages")
    else:
        print("     ⚠ 命令分布不干净（%d 种），可能夹错线、采样率太低、或抓的不是这块屏。"
              % r["ncmd"])


if __name__ == "__main__":
    main()
