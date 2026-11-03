#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_test_csv.py — 合成一份 ST7735S SPI 抓包 CSV，用来验证 st7735_decode.py

它会：
1. 画一张 160x128 的测试图案（渐变 + 色块 + 对角线 + 边框）；
2. 按 ST7735S 的初始化+绘图时序，生成 SPI 波形；
3. 故意把像素流拆成多个 CS 事务（模拟真实驱动的分块发送）；
4. 输出 capture.csv + expected.png，供逐像素比对。

用法：
    python make_test_csv.py out_dir [--spi-hz 6e6] [--sample-hz 24e6]
                          [--madctl 0x08] [--offset-x 0] [--offset-y 0]
                          [--split 3]
"""

import argparse
import os
import sys

try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    Image = None
    HAVE_PIL = False


# ---------------------------------------------------------------- 测试图案
def make_pattern(w, h):
    """返回 [(r,g,b)] * w*h"""
    px = bytearray(w * h * 3)
    for y in range(h):
        for x in range(w):
            # 底色：横向红->绿渐变，纵向叠加蓝
            r = int(255 * x / (w - 1))
            g = int(255 * y / (h - 1))
            b = int(255 * (1 - x / (w - 1)) * (y / (h - 1)))
            # 边框（白）
            if x < 2 or x >= w - 2 or y < 2 or y >= h - 2:
                r = g = b = 255
            # 中间 4 个纯色块
            if 20 <= x < 60 and 20 <= y < 50:
                r, g, b = 255, 0, 0
            if 70 <= x < 110 and 20 <= y < 50:
                r, g, b = 0, 255, 0
            if 20 <= x < 60 and 60 <= y < 90:
                r, g, b = 0, 0, 255
            if 70 <= x < 110 and 60 <= y < 90:
                r, g, b = 255, 255, 0
            # 对角线
            if abs(x - y * w // h) < 2 or abs((w - 1 - x) - y * w // h) < 2:
                r, g, b = 255, 0, 255
            o = (y * w + x) * 3
            px[o], px[o + 1], px[o + 2] = r, g, b
    return px


def rgb888_to_565(px, w, h, bgr=False):
    """把 RGB888 量化成 565。

    返回 (expected, vals)：
      expected —— 期望图（永远是真实颜色，供逐像素比对）
      vals     —— 真正写到总线上的 16 位值；bgr=True 时按 BGR 面板口径交换 R/B
                  （对应 MADCTL 的 D3=1，即面板把高 5 位当蓝色解释）
    """
    out = bytearray(len(px))
    vals = []
    for i in range(0, len(px), 3):
        r, g, b = px[i], px[i + 1], px[i + 2]
        v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        rr = (v >> 11) & 0x1F
        gg = (v >> 5) & 0x3F
        bb = v & 0x1F
        out[i] = (rr << 3) | (rr >> 2)
        out[i + 1] = (gg << 2) | (gg >> 4)
        out[i + 2] = (bb << 3) | (bb >> 2)
        vals.append((((b >> 3) << 11) | ((g >> 2) << 5) | (r >> 3)) if bgr else v)
    return out, vals


# ---------------------------------------------------------------- 波形合成
class WaveWriter:
    """按采样点写 CSV。支持 4 种 SPI 模式、CS/DC 极性可配。"""

    def __init__(self, path, sample_hz, spi_hz, mode=0, cs_active_low=True, dc_command_low=True):
        self.f = open(path, "w", encoding="utf-8", newline="")
        self.f.write("time,scl,sda,csx,dcx\n")
        self.spb = max(2, int(round(sample_hz / spi_hz)))
        self.spb += self.spb % 2          # 取偶数，半周期对齐
        self.dt = 1.0 / sample_hz
        self.n = 0
        self.buf = []
        self._flush_threshold = 20000

        self.cpol = (mode >> 1) & 1
        self.cpha = mode & 1
        # 每 bit 的前半周期时钟电平：mode0/3 -> 低，mode1/2 -> 高
        self.half1 = 0 if mode in (0, 3) else 1
        self.cs_idle = 1 if cs_active_low else 0
        self.cs_act = 0 if cs_active_low else 1
        self.dc_cmd = 0 if dc_command_low else 1

    def _dc(self, is_data):
        return (1 - self.dc_cmd) if is_data else self.dc_cmd

    def _emit(self, scl, sda, cs, dc):
        t = self.n * self.dt
        self.buf.append("%.9f,%d,%d,%d,%d\n" % (t, scl, sda, cs, dc))
        self.n += 1
        if len(self.buf) >= self._flush_threshold:
            self.f.write("".join(self.buf))
            self.buf.clear()

    def idle(self, count, cs=None, dc=None, sda=0):
        cs = self.cs_idle if cs is None else cs
        dc = self.dc_cmd if dc is None else dc
        for _ in range(count):
            self._emit(self.cpol, sda, cs, dc)

    def begin(self, idle=8):
        self.idle(idle)

    def end(self, idle=8):
        self.idle(idle)

    def transfer(self, payload, is_data_idle=0):
        """一个 CS 事务：片选拉低 -> 逐字节 -> 拉高。payload=[(value, is_data)]"""
        dc0 = self._dc(is_data_idle)
        self._emit(self.cpol, 0, self.cs_idle, dc0)
        self._emit(self.cpol, 0, self.cs_act, dc0)         # CS 有效
        half = self.spb // 2
        other = 1 - self.half1
        for value, is_data in payload:
            dc = self._dc(is_data)
            bits = [(value >> b) & 1 for b in range(7, -1, -1)]     # MSB first
            for i, b in enumerate(bits):
                nxt = bits[i + 1] if i + 1 < len(bits) else b
                prev = bits[i - 1] if i > 0 else b
                if self.cpha == 0:
                    # 数据在采样沿（前半周期结束）之前就稳定，之后立刻切下一位
                    for _ in range(half):
                        self._emit(self.half1, b, self.cs_act, dc)
                    self._emit(other, b, self.cs_act, dc)
                    for _ in range(half - 1):
                        self._emit(other, nxt, self.cs_act, dc)
                else:
                    # 数据在前沿切换：第一个采样点还留着上一位，之后才是本位
                    self._emit(self.half1, prev, self.cs_act, dc)
                    for _ in range(half - 1):
                        self._emit(self.half1, b, self.cs_act, dc)
                    for _ in range(half):
                        self._emit(other, b, self.cs_act, dc)
        # 释放 CS 前把时钟带回空闲电平（多出的边沿不会造成误采样）
        for _ in range(max(1, self.spb // 2)):
            self._emit(self.cpol, 0, self.cs_act, dc0)
        self._emit(self.cpol, 0, self.cs_idle, dc0)
        self.idle(6)

    def close(self):
        self.f.write("".join(self.buf))
        self.buf.clear()
        self.f.close()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("--width", type=int, default=160)
    ap.add_argument("--height", type=int, default=128)
    ap.add_argument("--sample-hz", type=float, default=24e6)
    ap.add_argument("--spi-hz", type=float, default=6e6)
    ap.add_argument("--madctl", type=lambda s: int(s, 0), default=0x08)
    ap.add_argument("--offset-x", type=int, default=0)
    ap.add_argument("--offset-y", type=int, default=0)
    ap.add_argument("--split", type=int, default=3, help="像素流拆成几个 CS 事务")
    ap.add_argument("--no-madctl", action="store_true", help="不发 0x36")
    ap.add_argument("--spi-mode", type=int, default=0, choices=(0, 1, 2, 3))
    ap.add_argument("--byte-order", choices=("big", "little"), default="big")
    ap.add_argument("--partial", default=None,
                    help='主帧之后再刷一块局部区域，格式 "x,y,w,h,RRGGBB"')
    ap.add_argument("--cs-active", choices=("low", "high"), default="low")
    ap.add_argument("--dc-command", choices=("low", "high"), default="low")
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    w, h = a.width, a.height
    ox, oy = a.offset_x, a.offset_y

    px = make_pattern(w, h)
    is_bgr = (not a.no_madctl) and bool(a.madctl & 0x08)
    expected, vals565 = rgb888_to_565(px, w, h, bgr=is_bgr)

    if HAVE_PIL:
        Image.frombytes("RGB", (w, h), bytes(expected)).save(
            os.path.join(a.outdir, "expected.png"))
    else:
        with open(os.path.join(a.outdir, "expected.ppm"), "wb") as f:
            f.write(b"P6\n%d %d\n255\n" % (w, h))
            f.write(bytes(expected))

    csv_path = os.path.join(a.outdir, "capture.csv")
    wv = WaveWriter(csv_path, a.sample_hz, a.spi_hz,
                    mode=a.spi_mode,
                    cs_active_low=(a.cs_active == "low"),
                    dc_command_low=(a.dc_command == "low"))
    wv.begin()

    # --- 初始化序列（真实驱动大概就是这样）---
    wv.transfer([(0x01, 0)], 0)                              # SWRESET
    wv.idle(200, cs=1)
    wv.transfer([(0x11, 0)], 0)                              # SLPOUT
    wv.idle(200, cs=1)
    wv.transfer([(0x3A, 0), (0x05, 1)], 0)                   # COLMOD = 16bit
    if not a.no_madctl:
        wv.transfer([(0x36, 0), (a.madctl & 0xFF, 1)], 0)    # MADCTL
    wv.transfer([(0x21, 0)], 0)                              # INVON
    wv.transfer([(0x13, 0)], 0)                              # NORON
    wv.transfer([(0x29, 0)], 0)                              # DISPON

    # --- 设置窗口（注意带偏移）---
    xs, xe = ox, ox + w - 1
    ys, ye = oy, oy + h - 1
    wv.transfer([(0x2A, 0), ((xs >> 8) & 0xFF, 1), (xs & 0xFF, 1),
                 ((xe >> 8) & 0xFF, 1), (xe & 0xFF, 1)], 0)
    wv.transfer([(0x2B, 0), ((ys >> 8) & 0xFF, 1), (ys & 0xFF, 1),
                 ((ye >> 8) & 0xFF, 1), (ye & 0xFF, 1)], 0)

    # --- RAMWR：命令单独一个事务，像素拆成 split 个事务 ---
    wv.transfer([(0x2C, 0)], 0)

    n = len(vals565)
    splits = max(1, a.split)
    step = (n + splits - 1) // splits
    for s in range(splits):
        chunk = vals565[s * step:(s + 1) * step]
        if not chunk:
            continue
        payload = []
        for v in chunk:
            hi, lo = (v >> 8) & 0xFF, v & 0xFF
            if a.byte_order == "big":
                payload.append((hi, 1))
                payload.append((lo, 1))
            else:
                payload.append((lo, 1))
                payload.append((hi, 1))
        wv.transfer(payload, 1)

    # --- 可选：第二次 RAMWR，只刷一个局部窗口（模拟 UI 局部刷新）---
    if a.partial:
        px0, py0, pw, ph, hexcolor = a.partial.split(",")
        px0, py0, pw, ph = int(px0), int(py0), int(pw), int(ph)
        c = int(hexcolor, 16)
        v = (((c >> 16) & 0xFF) >> 3) << 11 | (((c >> 8) & 0xFF) >> 2) << 5 | ((c & 0xFF) >> 3)
        if is_bgr:      # 面板按 BGR 解释，高 5 位放蓝色
            v = ((c & 0xFF) >> 3) << 11 | (((c >> 8) & 0xFF) >> 2) << 5 | (((c >> 16) & 0xFF) >> 3)
        xs2, xe2 = px0 + ox, px0 + ox + pw - 1
        ys2, ye2 = py0 + oy, py0 + oy + ph - 1
        wv.transfer([(0x2A, 0), ((xs2 >> 8) & 0xFF, 1), (xs2 & 0xFF, 1),
                     ((xe2 >> 8) & 0xFF, 1), (xe2 & 0xFF, 1)], 0)
        wv.transfer([(0x2B, 0), ((ys2 >> 8) & 0xFF, 1), (ys2 & 0xFF, 1),
                     ((ye2 >> 8) & 0xFF, 1), (ye2 & 0xFF, 1)], 0)
        wv.transfer([(0x2C, 0)], 0)
        payload = []
        for _ in range(pw * ph):
            hi, lo = (v >> 8) & 0xFF, v & 0xFF
            payload.append((hi, 1) if a.byte_order == "big" else (lo, 1))
            payload.append((lo, 1) if a.byte_order == "big" else (hi, 1))
        wv.transfer(payload, 1)
        print("附加局部刷新: (%d,%d) %dx%d 颜色 #%s" % (px0, py0, pw, ph, hexcolor))

    wv.end(64)
    wv.close()

    print("已生成: %s (%d 采样点, 每位 %d 点 -> SPI %.2f MHz, mode%d)"
          % (csv_path, wv.n, wv.spb, a.sample_hz / wv.spb / 1e6, a.spi_mode))
    print("期望图: %s" % os.path.join(a.outdir, "expected.png" if HAVE_PIL else "expected.ppm"))


if __name__ == "__main__":
    main()
