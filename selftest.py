#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归自测：多组参数合成波形 → --auto 解码 → 与 expected.png 逐像素比对。

MADCTL 的镜像位会让画面按面板方向翻转，所以比对时允许 4 种正交变换中取最优。
"""
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image

PY = sys.executable
HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, "make_test_csv.py")
DEC = os.path.join(HERE, "st7735_decode.py")

CASES = [
    ("默认(mode0/BGR/拆1)", []),
    ("mode1", ["--spi-mode", "1"]),
    ("mode2", ["--spi-mode", "2"]),
    ("mode3", ["--spi-mode", "3"]),
    ("CS 高有效", ["--cs-active", "high"]),
    ("DC 高=命令", ["--dc-command", "high"]),
    ("MADCTL 0x00", ["--madctl", "0x00"]),
    ("MADCTL 0xC0", ["--madctl", "0xC0"]),
    ("小端字节序", ["--byte-order", "little"]),
    ("偏移 (2,1)", ["--offset-x", "2", "--offset-y", "1"]),
    ("拆 3 段", ["--split", "3"]),
    ("拆 5 段", ["--split", "5"]),
    ("局部刷新(补丁区)", ["--partial", "20,30,40,24,FF8800"]),
    ("极限组合", ["--spi-mode", "3", "--cs-active", "high", "--dc-command", "high",
                  "--offset-x", "2", "--offset-y", "1", "--madctl", "0xC8",
                  "--byte-order", "little", "--split", "4"]),
]

def best_diff(a, b):
    cands = [b, b[::-1, ::-1], b[:, ::-1], b[::-1, :]]
    return min(float((np.abs(a - c).sum(2) > 0).mean() * 100) for c in cands)

ok = fail = 0
for name, extra in CASES:
    d = tempfile.mkdtemp(prefix="st_")
    try:
        r = subprocess.run([PY, GEN, d] + extra, capture_output=True, text=True)
        if r.returncode:
            print("%-22s 生成失败: %s" % (name, r.stderr.strip().splitlines()[-1:]))
            fail += 1
            continue
        out = os.path.join(d, "out.png")
        r = subprocess.run([PY, DEC, os.path.join(d, "capture.csv"), "--auto",
                            "-o", out], capture_output=True, text=True)
        if r.returncode:
            print("%-22s 解码失败: %s" % (name, r.stderr.strip().splitlines()[-1:]))
            fail += 1
            continue
        a = np.asarray(Image.open(out).convert("RGB")).astype(int)
        b = np.asarray(Image.open(os.path.join(d, "expected.png")).convert("RGB")).astype(int)
        if a.shape != b.shape:
            print("%-22s 尺寸不符 %s vs %s" % (name, a.shape, b.shape))
            fail += 1
            continue

        # 局部刷新：期望图只有底图，差异必须恰好落在补丁矩形内且颜色正确
        if "--partial" in extra:
            x, y, w, hh, hexc = extra[extra.index("--partial") + 1].split(",")
            x, y, w, hh = int(x), int(y), int(w), int(hh)
            mask = (np.abs(a - b).sum(2) > 0)
            ys, xs = np.nonzero(mask)
            inside = ((ys >= y) & (ys < y + hh) & (xs >= x) & (xs < x + w)).all()
            patch = a[y:y + hh, x:x + w].reshape(-1, 3)
            c = int(hexc, 16)
            q = lambda v, n: ((v >> n) << n) | ((v >> n) >> (8 - n)) if n == 3 else 0
            want = np.array([((c >> 16) & 0xFF) & 0xF8 | (((c >> 16) & 0xFF) >> 5),
                             ((c >> 8) & 0xFF) & 0xFC | (((c >> 8) & 0xFF) >> 6),
                             (c & 0xFF) & 0xF8 | ((c & 0xFF) >> 5)])
            if inside and (patch == want).all() and len(ys) == w * hh:
                print("%-22s 通过（补丁区 %d 像素精确命中）" % (name, w * hh))
                ok += 1
            else:
                print("%-22s !! 补丁区不符（差异 %d 像素，越界=%s）" % (name, len(ys), not inside))
                fail += 1
            continue

        pct = best_diff(a, b)
        if pct == 0.0:
            print("%-22s 通过（0 误差）" % name)
            ok += 1
        else:
            print("%-22s !! 差异 %.2f%%" % (name, pct))
            fail += 1
    finally:
        shutil.rmtree(d, ignore_errors=True)

print("\n通过 %d / 失败 %d" % (ok, fail))
sys.exit(0 if fail == 0 else 1)
