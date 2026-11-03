"""用合成波形验证 saleae_pages 的 v0 跳变列表解码（不碰硬件）。

流程：make_test_csv.py 造已知正确的 SPI 波形 → 转成 Saleae v0 二进制（每通道一个 .bin）
     → saleae_pages.decode_transitions + pages_from_pairs → 与 expected.png 逐像素比对。
"""
import os
import struct
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import saleae_pages as S

PY = sys.executable


def csv_to_v0(csv_path, out_dir, chans=(0, 1, 2, 3)):
    """把 make_test_csv 的 5 列逐样本 CSV 转成 4 个 v0 bin。

    CSV 列顺序：time, scl, sda, csx, dcx  →  通道号映射 0=SCL 1=CS 2=DC 3=SDA
    """
    os.makedirs(out_dir, exist_ok=True)
    col_of = {0: 1, 1: 3, 2: 4, 3: 2}      # 通道 → CSV 列
    rows = []
    with open(csv_path, encoding="utf-8", errors="ignore") as f:
        f.readline()
        for line in f:
            p = line.strip().split(",")
            if len(p) < 5:
                continue
            try:
                rows.append((float(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4])))
            except ValueError:
                continue
    t = np.array([r[0] for r in rows])

    paths = {}
    for ch in chans:
        v = np.array([r[col_of[ch]] for r in rows], dtype=np.int8)
        change = np.flatnonzero(v[1:] != v[:-1]) + 1
        init = int(v[0])
        times = t[change]
        hdr = b"<SALEAE>" + struct.pack("<ii", 0, 0) + struct.pack("<I", init)
        hdr += struct.pack("<dd", float(t[0]), float(t[-1]))
        hdr += struct.pack("<Q", len(times))
        p = os.path.join(out_dir, "digital_%d.bin" % ch)
        with open(p, "wb") as fh:
            fh.write(hdr)
            fh.write(times.astype("<f8").tobytes())
        paths[ch] = p
    return paths


CASES = [
    ("Mode3 基本", ["--spi-mode", "3", "--offset-x", "1", "--offset-y", "2"], {}),
    ("Mode3 拆4段", ["--spi-mode", "3", "--split", "4", "--offset-x", "1", "--offset-y", "2"], {}),
    ("Mode1 CS高 DC命令高", ["--spi-mode", "1", "--cs-active", "high", "--dc-command", "high",
                            "--offset-x", "1", "--offset-y", "2"],
     {"cs_active_low": False, "dc_command_low": False, "sample_on_rising": False}),
]


def main():
    ok = total = 0
    for name, opts, kw in CASES:
        total += 1
        d = tempfile.mkdtemp(prefix="v0test")
        r = subprocess.run([PY, os.path.join(HERE, "make_test_csv.py"), d] + opts,
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("%-22s 生成失败: %s" % (name, r.stderr[-200:]))
            continue
        paths = csv_to_v0(os.path.join(d, "capture.csv"), d)
        pairs, st = S.decode_transitions(paths, **kw)
        pages, pstat = S.pages_from_pairs(pairs)
        info = "字节%d 事务%d 帧%s 页%d" % (st["bytes"], st["tx"],
                                            pstat.get("frames", 0), len(pages))
        if not pages:
            print("%-22s ✗ 无整帧  %s  %s" % (name, info, pstat.get("note", "")))
            continue
        from PIL import Image
        a = np.asarray(pages[-1].convert("RGB")).astype(int)
        b = np.asarray(Image.open(os.path.join(d, "expected.png")).convert("RGB")).astype(int)
        if a.shape != b.shape:
            print("%-22s ✗ 尺寸 %s vs %s" % (name, a.shape, b.shape))
            continue
        best = 100.0
        for nm, arr in (("原样", b), ("转向", b[::-1, ::-1]), ("横翻", b[:, ::-1]),
                        ("竖翻", b[::-1, :]), ("转置", b.transpose(1, 0, 2))):
            if arr.shape != a.shape:
                continue
            best = min(best, float((np.abs(a - arr).max(axis=2) > 8).mean() * 100))
        mark = "✓ 逐像素一致" if best < 0.01 else ("△ 差异 %.2f%%" % best)
        if best < 0.01:
            ok += 1
        print("%-22s %s  %s" % (name, mark, info))
    print("\n%d/%d 通过" % (ok, total))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
