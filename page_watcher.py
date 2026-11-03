"""按键翻页 → 自动截图。

原理：Saleae Logic 2 的自动化接口（gRPC:10430）反复做短抓取，每次抓 1.2 秒，
      用本仓已验证的解码器还原成 160x128 图片；与上一页逐像素比较，
      变化超过阈值就当作新页面存成 pages/page_NNN.png。

用法：
    python page_watcher.py                # 开始监控（你只管按按键）
    python page_watcher.py --list         # 只列设备，不抓
    Ctrl+C 结束，会自动生成 pages/index.html 缩略图索引

前提：Logic 2 已打开，且 Preferences 底部的 automation 复选框已勾选。
"""
import argparse
import contextlib
import io
import os
import shutil
import sys
import tempfile
import time

import st7735_decode as D

# 这块板子实测出来的定案参数（见 README §2e），不用再自动侦测
LCD_W, LCD_H = 160, 128
OFF_X, OFF_Y = 1, 2
SCL_CH, SDA_CH, CS_CH, DC_CH = 0, 3, 1, 2


def connect():
    """连上正在运行的 Logic 2（必须绕过系统代理，否则 gRPC 会走 http_proxy）。"""
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        os.environ.pop(k, None)
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    from saleae import automation
    return automation


def decode_segments(csv_path, verbose=False):
    """把一次抓取切成若干"页面"，每页返回一张 PIL.Image。

    切法：每当总线画出**一整帧**（窗口覆盖整个面板且像素写满），就认为是一次
    整屏重绘 = 换页边界。两次整帧之间的局部写入（比如电量图标）归到前一页。

    这样一次抓 10 秒就能覆盖你在这个窗口里翻过的好几页，不用卡着节奏抓。
    """
    ap = D.build_argparser()
    argv = [
        csv_path, "-o", os.devnull,
        "--scl-col", str(SCL_CH), "--sda-col", str(SDA_CH),
        "--cs-col", str(CS_CH), "--dc-col", str(DC_CH),
        "--spi-mode", "3", "--bit-order", "msb",
        "--cs-active", "low", "--dc-command", "low",
        "--width", str(LCD_W), "--height", str(LCD_H),
        "--offset-x", str(OFF_X), "--offset-y", str(OFF_Y),
        "--madctl", "none", "--byte-order", "big", "--color-order", "rgb",
    ]
    args = ap.parse_args(argv)

    lcd = D.LcdParser(args)
    dec = D.SpiDecoder(D.SpiParams(cpol=1, cpha=1, msb_first=True,
                                   cs_active_low=True, dc_command_low=True),
                       lcd.on_byte)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        src, _kind = D.open_sample_source(csv_path, args)
        dec.feed(src)
        lcd.close()
    if verbose:
        sys.stderr.write(buf.getvalue())

    frames = lcd.frames
    if not frames:
        return [], "这段里总线上没有任何写屏数据"

    bounds = [i for i, f in enumerate(frames)
              if f.expected >= LCD_W * LCD_H and f.pix >= f.expected]
    if not bounds:
        best = max(frames, key=lambda f: f.expected)
        return [], ("这段里没有完整整帧（最大窗口 %dx%d 只写了 %d/%d）"
                    % (best.xe - best.xs + 1, best.ye - best.ys + 1,
                       best.pix, best.expected))

    if not D.HAVE_PIL:
        return [], "没装 Pillow，无法出图"
    from PIL import Image

    out = []
    for k, i in enumerate(bounds):
        end = bounds[k + 1] if k + 1 < len(bounds) else len(frames)
        rgb = D.composite(frames[i:end], LCD_W, LCD_H, (0, 0, 0))
        out.append(Image.frombytes("RGB", (LCD_W, LCD_H), bytes(rgb)))
    return out, "抓到 %d 个整帧 → %d 页" % (len(bounds), len(out))


def diff_pct(a, b, tol=8):
    """两张图差多少（超阈值的像素占比 %）。"""
    import numpy as np
    x = np.asarray(a, dtype=np.int16)
    y = np.asarray(b, dtype=np.int16)
    d = np.abs(x - y).max(axis=2)
    return float((d > tol).mean() * 100.0)


def make_index(out_dir, items):
    """生成缩略图索引页，方便一次性看全部页面。"""
    if not items:
        return None
    rows = []
    for i, (path, note) in enumerate(items):
        name = os.path.basename(path)
        rows.append('<figure><a href="%s"><img src="%s"></a>'
                    '<figcaption>#%d %s<br><small>%s</small></figcaption></figure>'
                    % (name, name, i + 1, name, note))
    html = """<!doctype html><meta charset="utf-8">
<title>MCU 全部页面截图</title>
<style>
body{background:#15181c;color:#e6e6e6;font:14px/1.6 "Microsoft YaHei",sans-serif;margin:24px}
h1{font-size:18px;font-weight:600;margin:0 0 4px}
p.hint{color:#8b949e;margin:0 0 20px}
.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:18px}
figure{margin:0;background:#1e2228;border:1px solid #2c323a;border-radius:8px;padding:10px;text-align:center}
img{width:100%%;image-rendering:pixelated;border-radius:4px;background:#000}
figcaption{margin-top:8px;font-size:12px;color:#c9d1d9}
small{color:#7d8590}
</style>
<h1>MCU 全部页面截图（%d 张）</h1>
<p class="hint">点任意一张看原图。左边第一张通常是开机默认页。</p>
<div class="g">%s</div>
""" % (len(items), "\n".join(rows))
    p = os.path.join(out_dir, "index.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(html)
    return p


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="按键翻页 → 自动截图")
    ap.add_argument("--out-dir", default="pages", help="截图输出目录（默认 pages）")
    ap.add_argument("--seconds", type=float, default=10.0,
                    help="每次抓取时长（默认 10s）。一轮里翻过的页都会切出来，"
                         "翻得快就把这个加大")
    ap.add_argument("--rate", type=int, default=16_000_000, help="采样率（默认 16MHz）")
    ap.add_argument("--min-diff", type=float, default=0.4,
                    help="与上一页差异超过多少 %% 才算新页面（默认 0.4）")
    ap.add_argument("--max-pages", type=int, default=200, help="最多存多少页")
    ap.add_argument("--minutes", type=float, default=0,
                    help="跑多少分钟自动停（默认 0 = 一直跑）")
    ap.add_argument("--list", action="store_true", help="只列出设备")
    ap.add_argument("--keep-csv", action="store_true", help="保留每轮 CSV（很占空间）")
    ap.add_argument("--verbose", action="store_true", help="打印解码器的完整日志")
    args = ap.parse_args()

    automation = connect()

    try:
        manager = automation.Manager.connect(port=10430)
    except Exception as e:
        print("连不上 Logic 2 的自动化接口（端口 10430）。")
        print("  请在 Logic 2 里：右上角齿轮 Preferences → 滚到最底部 →")
        print("  勾选 automation server 那个复选框，然后重跑本脚本。")
        print("  原始错误：%s" % str(e)[:160])
        return 2

    if args.list:
        try:
            for d in manager.get_devices():
                print(d)
        except Exception as e:
            print("列设备失败：%s" % e)
        manager.close()
        return 0

    dev_cfg = automation.LogicDeviceConfiguration(
        enabled_digital_channels=[SCL_CH, CS_CH, DC_CH, SDA_CH],
        digital_sample_rate=args.rate,
        digital_threshold_volts=3.3,
    )
    # 16MHz x 4 通道 x 10s ≈ 80MB，缓冲区显式放大，否则抓不满会被截断
    need_mb = int(args.rate * args.seconds * 4 / 8 / 1e6 * 2) + 64
    cap_cfg = automation.CaptureConfiguration(
        buffer_size_megabytes=max(64, min(need_mb, 1024)),
        capture_mode=automation.TimedCaptureMode(duration_seconds=args.seconds),
    )

    os.makedirs(args.out_dir, exist_ok=True)
    tmp_root = os.path.join(args.out_dir, "_raw")
    os.makedirs(tmp_root, exist_ok=True)

    saved = []          # [(png_path, note, PIL.Image)]
    misses = 0
    round_no = 0

    print("=" * 62)
    print("开始监控：现在可以按设备上的按键翻页了")
    print("  每轮抓 %.0f 秒（%.0f MHz），一轮里翻过的每一页都会被切出来" % (args.seconds, args.rate / 1e6))
    print("  输出目录：%s" % os.path.abspath(args.out_dir))
    print("  按 Ctrl+C 结束并生成总览页")
    print("=" * 62)

    try:
        deadline = (time.time() + args.minutes * 60) if args.minutes > 0 else None
        stop_file = os.path.join(args.out_dir, "STOP")
        while len(saved) < args.max_pages:
            if deadline and time.time() > deadline:
                print("\n到达时限，停止。")
                break
            if os.path.exists(stop_file):
                print("\n检测到 %s，停止。" % stop_file)
                break
            round_no += 1
            t0 = time.time()
            tmp = tempfile.mkdtemp(prefix="cap", dir=tmp_root)
            try:
                with manager.start_capture(device_configuration=dev_cfg,
                                           capture_configuration=cap_cfg) as cap:
                    cap.wait()
                    cap.export_raw_data_csv(
                        directory=tmp,
                        digital_channels=[SCL_CH, CS_CH, DC_CH, SDA_CH],
                    )
                csvs = [os.path.join(tmp, f) for f in os.listdir(tmp)
                        if f.lower().endswith(".csv")]
                if not csvs:
                    print("  [%02d] 这轮没导出 CSV，跳过" % round_no)
                    continue
                imgs, note = decode_segments(max(csvs, key=os.path.getsize),
                                             verbose=args.verbose)
            except Exception as e:
                # 单轮失败（设备忙 / 缓冲不足 / 导出超时）不该把监控整死
                print("  [%02d] 抓取失败：%s: %s"
                      % (round_no, type(e).__name__, str(e)[:160]))
                time.sleep(1.0)
                continue
            finally:
                if not args.keep_csv:
                    shutil.rmtree(tmp, ignore_errors=True)

            if not imgs:
                misses += 1
                print("  [%02d] 跳过：%s" % (round_no, note))
                continue

            added = 0
            for img in imgs:
                # 和已存的每一页比，都不同才算新页面（防止翻回去重复存）
                nearest = 100.0
                if saved:
                    nearest = min(diff_pct(p, img) for _, _, p in saved)
                if nearest < args.min_diff:
                    continue
                if len(saved) >= args.max_pages:
                    break
                path = os.path.join(args.out_dir, "page_%03d.png" % (len(saved) + 1))
                img.save(path)
                saved.append((path, time.strftime("%H:%M:%S"), img))
                added += 1
                print("    ✓ 新页面 #%d  %s  （与最近的一页差 %.1f%%）"
                      % (len(saved), os.path.basename(path), nearest))

            if added == 0:
                print("  [%02d] %s，都是已有页面（用时 %.1fs）"
                      % (round_no, note, time.time() - t0))
            else:
                print("  [%02d] %s，新增 %d 页，累计 %d 页（用时 %.1fs）"
                      % (round_no, note, added, len(saved), time.time() - t0))
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        try:
            manager.close()
        except Exception:
            pass

    print("\n共保存 %d 个页面：%s" % (len(saved), args.out_dir))
    idx = make_index(args.out_dir, [(p, t) for p, t, _ in saved])
    if idx:
        print("总览页：%s" % idx)
    if misses:
        print("（有 %d 轮没抓到完整整帧，通常是那一瞬间画面在滚动 / 刚上电）" % misses)
    return 0


if __name__ == "__main__":
    sys.exit(main())
