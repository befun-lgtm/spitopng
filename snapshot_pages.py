"""按时间密集快照切页（不依赖"整屏窗口"）。

为什么需要：这块板刷屏大量是**局部条带**写入（一次整屏 = 很多次小窗口）。
如果只认"窗口=整屏"当换页边界，那些只刷条带的换页就会被漏掉。
做法：按帧序增量合成画布，每 N 帧取一张快照，再按相似度去重。
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import l2cap
import st7735_decode as D

LCD_W, LCD_H = l2cap.LCD_W, l2cap.LCD_H


def load_frames(bin_dir):
    paths = {ch: os.path.join(bin_dir, "ch%d.bin" % ch) for ch in l2cap.CHANNELS}
    args = D.build_argparser().parse_args([
        os.devnull, "-o", os.devnull,
        "--width", str(LCD_W), "--height", str(LCD_H),
        "--offset-x", str(l2cap.OFF_X), "--offset-y", str(l2cap.OFF_Y),
        "--madctl", "none", "--byte-order", "big", "--color-order", "rgb",
    ])
    lcd = D.LcdParser(args)
    pairs, st = l2cap.decode_bytes(paths)
    for v, is_data in pairs:
        lcd.on_byte(v, is_data, 1)
    lcd.close()
    return lcd.frames, st


def snapshots(frames, stride=60):
    """增量合成画布，每 stride 帧产出一张 (帧序号, ndarray)。"""
    canvas = np.zeros((LCD_H, LCD_W, 3), dtype=np.uint8)
    out = []
    for i, f in enumerate(frames):
        cov = np.frombuffer(bytes(f.cover), dtype=np.uint8).reshape(LCD_H, LCD_W) > 0
        src = np.frombuffer(bytes(f.canvas), dtype=np.uint8).reshape(LCD_H, LCD_W, 3)
        canvas[cov] = src[cov]
        if (i + 1) % stride == 0:
            out.append((i + 1, canvas.copy()))
    if frames:
        out.append((len(frames), canvas.copy()))
    return out


def diff_pct(a, b, tol=8):
    return float((np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2) > tol).mean() * 100)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bins", default="_conv")
    ap.add_argument("--out-dir", default="pages2")
    ap.add_argument("--stride", type=int, default=60)
    ap.add_argument("--min-diff", type=float, default=1.0)
    args = ap.parse_args()

    frames, st = load_frames(args.bins)
    print("帧数 %d，字节 %d" % (len(frames), st.get("bytes", 0)), flush=True)
    snaps = snapshots(frames, args.stride)
    print("快照 %d 张（每 %d 帧一张）" % (len(snaps), args.stride), flush=True)

    os.makedirs(args.out_dir, exist_ok=True)
    kept = []
    for idx, img in snaps:
        nearest = 100.0
        for _, k in kept:
            nearest = min(nearest, diff_pct(k, img))
        if nearest < args.min_diff:
            continue
        kept.append((idx, img))

    for n, (idx, img) in enumerate(kept, 1):
        Image.fromarray(img).save(os.path.join(args.out_dir, "p%03d_f%05d.png" % (n, idx)))
    print("去重后 %d 页 → %s" % (len(kept), args.out_dir), flush=True)
    for n, (idx, img) in enumerate(kept, 1):
        print("  p%03d  第 %d 帧" % (n, idx), flush=True)


if __name__ == "__main__":
    main()
