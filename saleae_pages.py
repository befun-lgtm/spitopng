"""Saleae Logic 2 自动化抓包 → LCD 页面截图。

关键事实（已实测）：
  * `export_raw_data_binary` 对 Logic 8 导出的是 **version 0 跳变列表**，不是逐采样位图。
    文件 = 44 字节头 + 每个跳变 8 字节 double 绝对时间。
    所以一段 30 秒的抓取通常只有几百 KB ~ 几 MB，解码极快。
  * 导出是**非阻塞**的：RPC 返回只代表"开始写"，必须轮询文件大小直到稳定。
  * `Manager` 必须用 with 块，否则官方明说会有未定义行为（导不出数据）。
  * `buffer_size_megabytes` 必须给足（实测 64MB 连 1.2MB 数据都报满，
    给 3072MB 才正常）。这台机器 UI 里显示 0.03GB 是残留的旧值。

用法：
    python saleae_pages.py --seconds 20 --rate 8000000
    python saleae_pages.py --list          # 只列设备
"""
import argparse
import os
import shutil
import struct
import sys
import time

import numpy as np

import st7735_decode as D

# ---- 这块板的实测参数（见 README §2e）----
LCD_W, LCD_H = 160, 128
OFF_X, OFF_Y = 1, 2
SCL_CH, SDA_CH, CS_CH, DC_CH = 0, 3, 1, 2
CHANNELS = [SCL_CH, CS_CH, DC_CH, SDA_CH]

DEVICE_ID = "A7D1BB81883C0092"
BUFFER_MB = 3072


# ---------------------------------------------------------------- 连接
def connect():
    """连 Logic 2。必须绕开系统代理，否则 gRPC 走 http_proxy 连不上。"""
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY"):
        os.environ.pop(k, None)
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    from saleae import automation
    return automation


# ---------------------------------------------------------------- bin 解析
def load_transitions(path):
    """读 version-0 数字二进制导出。返回 (initial_state, begin, end, times[])。"""
    raw = open(path, "rb").read()
    if len(raw) < 44 or raw[:8] != b"<SALEAE>":
        raise ValueError("不是 Saleae v0 二进制文件: %s" % os.path.basename(path))
    ver, typ = struct.unpack("<ii", raw[8:16])
    init = struct.unpack("<I", raw[16:20])[0]
    begin, end = struct.unpack("<dd", raw[20:36])
    nt = struct.unpack("<Q", raw[36:44])[0]
    avail = (len(raw) - 44) // 8
    if nt > avail:                    # 写盘还没完，按实际长度截
        nt = avail
    times = np.frombuffer(raw, dtype="<f8", count=nt, offset=44).astype(np.float64)
    return int(init), float(begin), float(end), times


def level_at(times, init, query):
    """在 query 这些时刻上取通道电平（0/1）。times 必须升序。"""
    if len(times) == 0:
        return np.full(len(query), init, dtype=np.int8)
    idx = np.searchsorted(times, query, side="right")     # 已经翻过几次
    return ((init ^ (idx & 1)) & 1).astype(np.int8)


def decode_transitions(files, cs_active_low=True, dc_command_low=True,
                       msb_first=True, sample_on_rising=True):
    """四个通道的跳变列表 → [(byte, is_data)]。

    与 st7735_decode.SpiDecoder 同一口径：CS 有效段内，每 8 个采样沿组一个字节，
    字节的命令/数据属性取第 8 个沿时刻的 DC 电平。
    """
    init_c, _, _, t_c = load_transitions(files[SCL_CH])   # 时钟（通道号 -> 路径）
    init_s, _, _, t_s = load_transitions(files[CS_CH])
    init_d, _, _, t_d = load_transitions(files[DC_CH])
    init_a, _, _, t_a = load_transitions(files[SDA_CH])

    # 时钟采样沿：上升沿 or 下降沿
    if sample_on_rising:
        rise = t_c[0::2] if init_c == 0 else t_c[1::2]
    else:
        rise = t_c[1::2] if init_c == 0 else t_c[0::2]
    if len(rise) == 0:
        return [], dict(edges=0, bytes=0, tx=0)

    cs_at = level_at(t_s, init_s, rise)
    dc_at = level_at(t_d, init_d, rise)
    sda_at = level_at(t_a, init_a, rise)

    active = (cs_at == 0) if cs_active_low else (cs_at == 1)
    keep = np.flatnonzero(active)
    if len(keep) == 0:
        return [], dict(edges=len(rise), bytes=0, tx=0)

    sel = np.zeros(len(rise), dtype=bool)
    sel[keep] = True
    # 连续有效段分组：段内每 8 个沿成字节
    bounds = np.flatnonzero(np.diff(sel.astype(np.int8)) != 0) + 1
    seg_starts = np.concatenate(([0], bounds))
    seg_ends = np.concatenate((bounds, [len(sel)]))

    out = []
    n_tx = 0
    for a, b in zip(seg_starts, seg_ends):
        if not sel[a]:
            continue
        bits = sda_at[a:b]
        dcs = dc_at[a:b]
        n = len(bits) // 8
        if n == 0:
            continue
        n_tx += 1
        for i in range(n):
            chunk = bits[i * 8:(i + 1) * 8]
            if msb_first:
                v = 0
                for bit in chunk:
                    v = (v << 1) | int(bit)
            else:
                v = 0
                for k, bit in enumerate(chunk):
                    v |= int(bit) << k
            dcv = int(dcs[i * 8 + 7])
            is_data = (dcv == 1) if dc_command_low else (dcv == 0)
            out.append((v, is_data))
    return out, dict(edges=len(rise), bytes=len(out), tx=n_tx)


# ---------------------------------------------------------------- 出图
def make_args(width=LCD_W, height=LCD_H, off_x=OFF_X, off_y=OFF_Y):
    return D.build_argparser().parse_args([
        os.devnull, "-o", os.devnull,
        "--width", str(width), "--height", str(height),
        "--offset-x", str(off_x), "--offset-y", str(off_y),
        "--madctl", "none", "--byte-order", "big",
    ])


def pages_from_pairs(pairs, width=LCD_W, height=LCD_H, off_x=OFF_X, off_y=OFF_Y):
    """字节流 → (页面图列表, 统计)。换页边界 = 窗口铺满整屏的 RAMWR。"""
    if not pairs:
        return [], {}
    args = make_args(width, height, off_x, off_y)
    lcd = D.LcdParser(args)
    for v, is_data in pairs:
        lcd.on_byte(v, is_data, 1)
    lcd.close()

    stat = dict(total_bytes=len(pairs), frames=len(lcd.frames),
                cmds=len(lcd.cmd_hist))
    pages = []
    if not lcd.frames:
        return [], stat
    full = [i for i, f in enumerate(lcd.frames)
            if f.expected >= width * height and f.pix >= f.expected]
    if not full:
        # 退路：没有整屏窗口时，若总像素足够也拼一帧
        biggest = max(lcd.frames, key=lambda f: f.expected)
        stat["note"] = "无整屏窗口（最大 %dx%d 只写 %d/%d）" % (
            biggest.xe - biggest.xs + 1, biggest.ye - biggest.ys + 1,
            biggest.pix, biggest.expected)
        return [], stat
    from PIL import Image
    for k, i in enumerate(full):
        end = full[k + 1] if k + 1 < len(full) else len(lcd.frames)
        rgb = D.composite(lcd.frames[i:end], width, height, (0, 0, 0))
        pages.append(Image.frombytes("RGB", (width, height), bytes(rgb)))
    stat["pages"] = len(pages)
    return pages, stat


# ---------------------------------------------------------------- 抓取
def capture(automation, manager, device_id, out_dir, seconds=20.0,
            rate=8_000_000, buffer_mb=BUFFER_MB, channels=None, verbose=True):
    """抓一段并导出 4 个通道的 .bin，返回 {通道: 路径}。

    注意 export 是非阻塞的 → 轮询文件大小直到稳定。
    """
    channels = channels or CHANNELS
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):                      # 清掉上次的残留
        if f.endswith(".bin"):
            os.remove(os.path.join(out_dir, f))

    dev = automation.LogicDeviceConfiguration(
        enabled_digital_channels=list(channels),
        digital_sample_rate=int(rate),
    )
    cfg = automation.CaptureConfiguration(
        buffer_size_megabytes=int(buffer_mb),
        capture_mode=automation.TimedCaptureMode(duration_seconds=float(seconds)),
    )
    cap = manager.start_capture(device_configuration=dev, device_id=device_id,
                                capture_configuration=cfg)
    try:
        cap.wait()
        cap.export_raw_data_binary(directory=out_dir, digital_channels=list(channels))
        # 等文件写完：连续 3 次大小不变才算稳定
        last, stable = -1, 0
        for _ in range(120):
            time.sleep(0.25)
            sz = 0
            for f in os.listdir(out_dir):
                if f.endswith(".bin"):
                    sz += os.path.getsize(os.path.join(out_dir, f))
            if sz == last and sz > 0:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable, last = 0, sz
        if verbose and stable < 3:
            print("  ! 导出等待超时（文件可能不完整）")
    finally:
        try:
            cap.close()
        except Exception:
            pass

    paths = {}
    for f in sorted(os.listdir(out_dir)):
        if not f.endswith(".bin"):
            continue
        parts = os.path.splitext(f)[0].split("_")
        try:
            ch = int(parts[-1])
        except ValueError:
            continue
        paths[ch] = os.path.join(out_dir, f)
    return paths


def diff_pct(a, b, tol=8):
    x = np.asarray(a, dtype=np.int16)
    y = np.asarray(b, dtype=np.int16)
    if x.shape != y.shape:
        return 100.0
    return float((np.abs(x - y).max(axis=2) > tol).mean() * 100.0)


# ---------------------------------------------------------------- 主循环
def watch(args):
    A = connect()
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, "_raw")
    saved = []          # [(文件名, 图)]
    rounds = 0
    misses = 0

    print("=" * 64)
    print("开始监控：现在可以按设备上的按键翻页")
    print("  每轮抓 %.1f 秒 @ %.0f MHz，画面一变就自动存一张" % (args.seconds, args.rate / 1e6))
    print("  输出目录：%s" % out_dir)
    if args.max_rounds:
        print("  最多抓 %d 轮" % args.max_rounds)
    print("=" * 64)

    with A.Manager.connect(port=args.port) as manager:
        devs = manager.get_devices()
        if not devs:
            print("! Logic 2 里没有设备。检查 USB 线、Logic 2 是否打开。")
            return 2
        dev_id = DEVICE_ID if any(d.device_id == DEVICE_ID for d in devs) else devs[0].device_id
        print("设备：%s" % dev_id)

        try:
            while True:
                if args.max_rounds and rounds >= args.max_rounds:
                    break
                rounds += 1
                t0 = time.time()
                try:
                    paths = capture(A, manager, dev_id, tmp,
                                    seconds=args.seconds, rate=args.rate,
                                    verbose=args.verbose)
                except Exception as e:
                    print("  [%02d] 抓取失败：%s" % (rounds, str(e).split("\n")[0][:100]))
                    time.sleep(1.0)
                    continue

                missing = [c for c in CHANNELS if c not in paths]
                if missing:
                    print("  [%02d] 缺少通道 %s，跳过" % (rounds, missing))
                    misses += 1
                    continue

                try:
                    pairs, st = decode_transitions(paths)
                except Exception as e:
                    print("  [%02d] 解码失败：%s" % (rounds, str(e)[:100]))
                    misses += 1
                    continue

                if st["bytes"] == 0:
                    print("  [%02d] 总线无数据（时钟沿 %d 个）—— 画面可能静止"
                          % (rounds, st["edges"]))
                    misses += 1
                    continue

                pages, pstat = pages_from_pairs(pairs)
                if not pages:
                    print("  [%02d] %d 字节但没有完整整帧（%s）"
                          % (rounds, st["bytes"], pstat.get("note", "只有局部刷新")))
                    misses += 1
                    continue

                for img in pages:
                    if saved and min(diff_pct(k, img) for _, k in saved) < args.min_diff:
                        continue
                    idx = len(saved) + 1
                    name = "page_%03d.png" % idx
                    img.save(os.path.join(out_dir, name))
                    saved.append((name, img))
                    print("  [%02d] ✓ %s  累计 %d 页  用时 %.1fs"
                          % (rounds, name, len(saved), time.time() - t0))
                    if len(saved) >= args.max_pages:
                        break
                if len(saved) >= args.max_pages:
                    break
                if pages and len(saved) < args.max_pages:
                    print("  [%02d] 画面未变化（%d 字节 / %d 帧）"
                          % (rounds, st["bytes"], pstat.get("frames", 0)))
        except KeyboardInterrupt:
            print("\n已停止。")

    print("\n共保存 %d 个页面 → %s" % (len(saved), out_dir))
    if misses:
        print("（%d 轮没有可用数据）" % misses)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Saleae 自动抓包 → LCD 页面截图")
    ap.add_argument("--seconds", type=float, default=20.0, help="每轮抓取秒数（默认 20）")
    ap.add_argument("--rate", type=int, default=8_000_000, help="采样率（默认 8MHz）")
    ap.add_argument("--port", type=int, default=10430)
    ap.add_argument("--out-dir", default="pages_auto")
    ap.add_argument("--min-diff", type=float, default=0.5, help="判定为新页面的差异阈值%%")
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--max-rounds", type=int, default=0, help="0 = 不限")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.list:
        A = connect()
        with A.Manager.connect(port=args.port) as m:
            for d in m.get_devices():
                print("%s  %s" % (d.device_id, d.device_type))
        return 0
    return watch(args)


if __name__ == "__main__":
    sys.exit(main())
