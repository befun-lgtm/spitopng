"""PulseView 8 通道边沿表 CSV → 打包位图，复用 l2cap 的向量化解码。

为什么可以这么干：这份 CSV 是"任一通道变化就写一行"的边沿表，
每一行代表**那一瞬间所有通道的新电平**。解码只需要"时钟上升沿那一刻
其它通道是什么电平"，所以把每行当作一个伪采样点，行序即时间序，
判定结果与逐样本解码完全等价。

列序：Time, Channel 0..7  →  Ch0=SCL  Ch1=CS  Ch2=DC  Ch3=SDA（这块板的实测接法）
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import l2cap


def csv_to_bins(csv_path, out_dir, cols=(0, 1, 2, 3), chunk_rows=4_000_000):
    """分块读取，边读边写，内存只占一块。返回 {角色通道: bin 路径}。"""
    os.makedirs(out_dir, exist_ok=True)
    tmp = {}
    role_of_col = {l2cap.SCL_CH: cols[0], l2cap.CS_CH: cols[1],
                   l2cap.DC_CH: cols[2], l2cap.SDA_CH: cols[3]}
    for ch in l2cap.CHANNELS:
        tmp[ch] = open(os.path.join(out_dir, "ch%d.rawbits" % ch), "wb")

    total = 0
    carry = {ch: np.zeros(0, dtype=np.uint8) for ch in l2cap.CHANNELS}
    header = True
    with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
        while True:
            rows = []
            for _ in range(chunk_rows):
                line = f.readline()
                if not line:
                    break
                if header:
                    header = False
                    if line.lower().startswith("time"):
                        continue
                p = line.rstrip("\n").rstrip("\r").split(",")
                if len(p) < 9:
                    continue
                rows.append(p[1:9])
            if not rows:
                break
            arr = np.array(rows, dtype=np.uint8)  # 0/1
            total += len(arr)
            for ch in l2cap.CHANNELS:
                col = arr[:, role_of_col[ch]]
                buf = np.concatenate((carry[ch], col)) if len(carry[ch]) else col
                n_full = (len(buf) // 8) * 8
                if n_full:
                    tmp[ch].write(np.packbits(buf[:n_full], bitorder="little").tobytes())
                carry[ch] = buf[n_full:].copy()
            del rows, arr

    paths = {}
    for ch in l2cap.CHANNELS:
        if len(carry[ch]):
            pad = np.zeros(8 - len(carry[ch]), dtype=np.uint8)
            tmp[ch].write(np.packbits(np.concatenate((carry[ch], pad)),
                                      bitorder="little").tobytes())
        tmp[ch].close()
        raw = os.path.join(out_dir, "ch%d.rawbits" % ch)
        dst = os.path.join(out_dir, "ch%d.bin" % ch)
        os.replace(raw, dst)
        paths[ch] = dst
    return paths, total


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--out-dir", default="_conv")
    ap.add_argument("--pages", default="pages")
    args = ap.parse_args()

    paths, n = csv_to_bins(args.csv, args.out_dir)
    print("读入 %d 行边沿" % n, flush=True)
    for ch, p in sorted(paths.items()):
        print("  ch%d %d 采样位" % (ch, os.path.getsize(p) * 8), flush=True)

    pages, note, st = l2cap.decode_bin_to_pages(paths, madctl="none")
    print("解码:", note, flush=True)
    print("统计:", {k: st.get(k) for k in ("rise", "bytes", "partial", "tx", "n_cmd", "frames")}, flush=True)
    print("命令分布:", list(st.get("cmds", {}).items())[:8], flush=True)

    os.makedirs(args.pages, exist_ok=True)
    for i, im in enumerate(pages):
        p = os.path.join(args.pages, "page_%03d.png" % (i + 1))
        im.save(p)
    print("共 %d 页 → %s" % (len(pages), args.pages), flush=True)


if __name__ == "__main__":
    main()
