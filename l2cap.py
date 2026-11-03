"""Logic 2 二进制抓包 → LCD 页面（快速通道）。

为什么不用 CSV：Logic 2 的 Raw CSV 是**按采样点**逐行写的，
16MHz x 3s 就是 4800 万行、约 2GB，导出会卡死（这就是刚才"没输出"的原因）。
二进制导出是 1 bit/采样，同样一段只有 6MB，快两个数量级。

二进制格式（Saleae 官方文档）：每个通道一个 .bin，1 bit/采样，
8 个采样打包成 1 字节，**低位在前**（bit0 = 最早那个采样），无文件头。

解码在本模块内用 numpy 向量化完成，逐字节结果与 st7735_decode.SpiDecoder
完全一致（同样的 CS 事务内每 8 个上升沿凑一字节、同样用第 8 位的 DC 电平）。
"""
import os
import shutil
import time

import numpy as np

import st7735_decode as D

LCD_W, LCD_H = 160, 128
OFF_X, OFF_Y = 1, 2
SCL_CH, SDA_CH, CS_CH, DC_CH = 0, 3, 1, 2
CHANNELS = [SCL_CH, CS_CH, DC_CH, SDA_CH]
DEVICE_ID = "A7D1BB81883C0092"


# ---------------------------------------------------------------- 连接
def connect():
    """连 Logic 2 的自动化端口。必须绕过系统代理，否则 gRPC 会走 http_proxy。"""
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY"):
        os.environ.pop(k, None)
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    from saleae import automation
    return automation


def open_manager(automation, port=10430):
    return automation.Manager.connect(port=port)


def pick_device(manager, want=DEVICE_ID):
    devs = manager.get_devices()
    if not devs:
        raise RuntimeError("Logic 2 里没有可用设备（检查 USB 线和 Logic 2 左下角设备状态）")
    for d in devs:
        if d.device_id == want:
            return d.device_id
    return devs[0].device_id


# ---------------------------------------------------------------- 抓取
def capture_bin(automation, manager, device_id, out_dir, seconds=3.0,
                rate=16_000_000, buffer_mb=None):
    """抓一段并存成 4 个 .bin（每通道一个目录，避免文件名歧义）。"""
    os.makedirs(out_dir, exist_ok=True)
    if buffer_mb is None:
        # 打包后每秒每通道 rate/8 字节，留 4 倍余量
        buffer_mb = max(64, int(rate * seconds * len(CHANNELS) / 8 / 1e6 * 3) + 32)

    dev = automation.LogicDeviceConfiguration(
        enabled_digital_channels=CHANNELS,
        digital_sample_rate=int(rate),
    )
    cfg = automation.CaptureConfiguration(
        buffer_size_megabytes=int(buffer_mb),
        capture_mode=automation.TimedCaptureMode(duration_seconds=float(seconds)),
    )

    paths = {}
    cap = manager.start_capture(device_configuration=dev, device_id=device_id,
                               capture_configuration=cfg)
    try:
        cap.wait()
        for ch in CHANNELS:
            d = os.path.join(out_dir, "ch%d" % ch)
            os.makedirs(d, exist_ok=True)
            cap.export_raw_data_binary(directory=d, digital_channels=[ch])
            bins = [f for f in os.listdir(d) if f.lower().endswith(".bin")]
            if not bins:
                raise RuntimeError("通道 %d 没有导出 .bin" % ch)
            paths[ch] = os.path.join(d, max(bins, key=lambda f: os.path.getsize(os.path.join(d, f))))
    finally:
        try:
            cap.close()
        except Exception:
            pass
    return paths


# ---------------------------------------------------------------- 位操作
def load_bits(path):
    return np.memmap(path, dtype=np.uint8, mode="r")


def n_samples(path):
    return len(load_bits(path)) * 8


def bits_at(path, idx):
    """取指定采样点上的电平（不展开整条流，省内存）。"""
    raw = load_bits(path)
    idx = np.asarray(idx, dtype=np.int64)
    return ((raw[idx >> 3] >> (idx & 7).astype(np.uint8)) & 1).astype(np.uint8)


def rising_edges(path, chunk_bytes=1 << 22, rising=True):
    """时钟边沿的采样点下标（分块展开，避免上百 MB 的中间数组）。

    rising=True 取 0→1（SPI Mode 0/3），False 取 1→0（Mode 1/2）。
    """
    raw = load_bits(path)
    out = []
    prev_last = None
    step = chunk_bytes
    for s in range(0, len(raw), step):
        blk = np.unpackbits(np.asarray(raw[s:s + step]), bitorder="little")
        if len(blk) == 0:
            continue
        if rising:
            r = np.flatnonzero((blk[1:] == 1) & (blk[:-1] == 0)) + 1
        else:
            r = np.flatnonzero((blk[1:] == 0) & (blk[:-1] == 1)) + 1
        base = s * 8
        r = r + base
        if prev_last is not None:
            hit = (prev_last == 0 and blk[0] == 1) if rising else (prev_last == 1 and blk[0] == 0)
            if hit:
                r = np.concatenate(([base], r))
        out.append(r)
        prev_last = int(blk[-1])
    return np.concatenate(out) if out else np.zeros(0, dtype=np.int64)


# ---------------------------------------------------------------- 解码
def decode_bytes(paths, dc_command_low=True, msb_first=True, cs_active_low=True,
                 sample_on_rising=True):
    """二进制样本 → [(byte_value, is_data), ...]（按时间顺序）。

    与 SpiDecoder 的规则逐条对齐：
      · 只在时钟上升沿取 SDA
      · CS 无效时不计；CS 每次拉低开始一个新事务，字节在事务内重新对齐
      · 攒满 8 bit 成 1 字节，MSB 先
      · 该字节的命令/数据属性取**第 8 个 bit 那个采样点**的 DC 电平
    """
    clk = paths[SCL_CH]
    rise = rising_edges(clk, rising=sample_on_rising)
    if len(rise) == 0:
        return [], {"rise": 0, "bytes": 0, "partial": 0}

    sda = bits_at(paths[SDA_CH], rise)
    cs = bits_at(paths[CS_CH], rise)
    dc = bits_at(paths[DC_CH], rise)

    cs_act = (cs == 0) if cs_active_low else (cs == 1)

    change = np.empty(len(cs_act), dtype=bool)
    change[0] = True
    change[1:] = cs_act[1:] != cs_act[:-1]
    run_id = np.cumsum(change) - 1

    sel = np.flatnonzero(cs_act)
    if len(sel) == 0:
        return [], {"rise": int(len(rise)), "bytes": 0, "partial": 0}

    # 只在 CS 有效的那部分时钟沿上取 SDA/DC，否则 sel 比 sda 短一个（下面 broadcast 会炸）
    sda = sda[sel]
    dc = dc[sel]

    run_sel = run_id[sel]
    starts = np.flatnonzero(np.r_[True, run_sel[1:] != run_sel[:-1]])
    counts = np.diff(np.r_[starts, len(sel)])
    pos = np.arange(len(sel)) - np.repeat(starts, counts)

    grp = pos // 8
    bitpos = (7 - (pos % 8)) if msb_first else (pos % 8)

    key = run_sel.astype(np.int64) * (1 << 20) + grp
    uniq, inv = np.unique(key, return_inverse=True)

    val = np.zeros(len(uniq), dtype=np.uint16)
    np.add.at(val, inv, (sda.astype(np.uint16) << bitpos.astype(np.uint16)))
    full = np.bincount(inv, minlength=len(uniq))

    dcb = np.zeros(len(uniq), dtype=np.uint8)
    lastbit = (bitpos == 0) if msb_first else (bitpos == 7)
    dcb[inv[lastbit]] = dc[lastbit]

    order = inv[lastbit]                      # 按时间顺序的"每字节结束"组号
    keep = [int(g) for g in order if full[g] >= 8]

    out = []
    for g in keep:
        is_data = 1 if ((int(dcb[g]) == 1) == bool(dc_command_low)) else 0
        out.append((int(val[g]), is_data))
    partial = int((full < 8).sum())
    stats = {"rise": int(len(rise)), "bytes": len(out), "partial": partial,
             "tx": int(len(starts))}
    return out, stats


def decode_bin_to_pages(paths, verbose=False, cs_active_low=True,
                        dc_command_low=True, msb_first=True, sample_on_rising=True,
                        width=None, height=None, off_x=None, off_y=None,
                        madctl="none", byte_order="big", color_order="rgb"):
    """二进制抓包 → (页面图列表, 说明, 统计)。"""
    w = LCD_W if width is None else width
    h = LCD_H if height is None else height
    ox = OFF_X if off_x is None else off_x
    oy = OFF_Y if off_y is None else off_y
    args = D.build_argparser().parse_args([
        os.devnull, "-o", os.devnull,
        "--width", str(w), "--height", str(h),
        "--offset-x", str(ox), "--offset-y", str(oy),
        "--madctl", madctl, "--byte-order", byte_order, "--color-order", color_order,
    ])
    lcd = D.LcdParser(args)
    pairs, stats = decode_bytes(paths, dc_command_low=dc_command_low,
                                msb_first=msb_first, cs_active_low=cs_active_low,
                                sample_on_rising=sample_on_rising)
    stats.setdefault("tx", 0)
    if not pairs:
        return [], "这段里总线上没有任何 SPI 字节", stats
    for v, is_data in pairs:
        lcd.on_byte(v, is_data, 1)
    lcd.close()
    stats["n_cmd"] = lcd.n_cmd_bytes
    stats["n_data"] = lcd.n_data_bytes
    stats["frames"] = len(lcd.frames)
    stats["cmds"] = dict(sorted(lcd.cmd_hist.items(), key=lambda kv: -kv[1])[:8])
    pages = pages_from_frames(lcd.frames, w, h)
    return pages, "字节 %d / 帧 %d → 页 %d" % (len(pairs), len(lcd.frames), len(pages)), stats


def pages_from_frames(frames, width=None, height=None):
    """按"整屏重绘"切页：窗口铺满面板且像素写满 = 一次换页边界。"""
    w = LCD_W if width is None else width
    h = LCD_H if height is None else height
    if not frames:
        return []
    bounds = [i for i, f in enumerate(frames)
              if f.expected >= w * h and f.pix >= f.expected]
    if not bounds:
        return []
    from PIL import Image
    out = []
    for k, i in enumerate(bounds):
        end = bounds[k + 1] if k + 1 < len(bounds) else len(frames)
        rgb = D.composite(frames[i:end], w, h, (0, 0, 0))
        out.append(Image.frombytes("RGB", (w, h), bytes(rgb)))
    return out
