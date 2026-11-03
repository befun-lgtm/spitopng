"""离线验证：把合成抓包打包成 .bin，再用 l2cap 解回来，与期望图逐像素比对。

这一步行验证的是二进制通路的位序、字节组装、切页逻辑，
与硬件无关（设备被占用也能跑）。
"""
import os
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import l2cap
import st7735_decode as D

PY = sys.executable

CASES = [
    ("Mode3 CS低 DC命令低",
     ["--spi-mode", "3", "--offset-x", "1", "--offset-y", "2"],
     {"madctl": "auto", "color_order": "auto"}),
    ("Mode3 拆4段",
     ["--spi-mode", "3", "--split", "4", "--offset-x", "1", "--offset-y", "2"],
     {"madctl": "auto", "color_order": "auto"}),
    ("Mode1 CS高 DC命令高",
     ["--spi-mode", "1", "--cs-active", "high", "--dc-command", "high",
      "--offset-x", "1", "--offset-y", "2"],
     {"cs_active_low": False, "dc_command_low": False, "sample_on_rising": False,
      "madctl": "auto", "color_order": "auto"}),
]


def build_bins(csv_path, out_dir):
    """把 5 列逐样本 CSV（time,scl,sda,cs,dc）打包成 4 个 .bin。"""
    rows = []
    with open(csv_path, encoding="utf-8", errors="ignore") as f:
        head = f.readline()
        for line in f:
            p = line.strip().split(",")
            if len(p) < 5:
                continue
            rows.append([int(float(x) >= 0.5) for x in p[1:5]])
    arr = np.asarray(rows, dtype=np.uint8)
    # CSV 列序 = scl, sda, cs, dc    → 我们的角色键
    mapping = {l2cap.SCL_CH: 0, l2cap.SDA_CH: 1, l2cap.CS_CH: 2, l2cap.DC_CH: 3}
    paths = {}
    for ch, col in mapping.items():
        d = os.path.join(out_dir, "ch%d" % ch)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "digital.bin")
        np.packbits(arr[:, col], bitorder="little").tofile(p)
        paths[ch] = p
    return paths, len(arr)


def main():
    ok = 0
    total = 0
    for name, opts, kw in CASES:
        total += 1
        d = tempfile.mkdtemp(prefix="bintest")
        print("=== %s ===" % name)
        r = subprocess.run([PY, os.path.join(HERE, "make_test_csv.py"), d] + opts,
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("  生成失败:", r.stderr[-300:])
            continue
        paths, n = build_bins(os.path.join(d, "capture.csv"), d)
        pages, note, stats = l2cap.decode_bin_to_pages(paths, **kw)
        print("  样本 %d → %s" % (n, note))
        print("  统计:", {k: stats[k] for k in ("rise", "bytes", "partial", "tx", "n_cmd", "frames")})
        print("  命令前 6:", list(stats.get("cmds", {}).items())[:6])
        if not pages:
            print("  没有页面")
            continue
        from PIL import Image
        a = np.asarray(pages[-1].convert("RGB")).astype(int)
        b = np.asarray(Image.open(os.path.join(d, "expected.png")).convert("RGB")).astype(int)
        best = 1e9
        for k, arr in (("原样", b), ("转置", b.transpose(1, 0, 2)),
                       ("180", b[::-1, ::-1]), ("水平", b[:, ::-1])):
            if arr.shape != a.shape:
                continue
            pct = float((np.abs(a - arr).max(axis=2) > 8).mean() * 100)
            best = min(best, pct)
            print("    %-4s 不同像素 %.2f%%" % (k, pct))
        if best < 0.01:
            print("  ✓ 逐像素一致")
            ok += 1
        else:
            print("  ✗ 最小差异 %.2f%%" % best)
    print("\n通过 %d/%d" % (ok, total))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
