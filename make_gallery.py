"""把已还原的页面整理成可浏览的页面集（含总览 index.html）。"""
import glob
import os
import shutil

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "pages")
OUT = os.path.join(HERE, "全部页面")

# 按抓包时间顺序的页面说明（人工核对过的内容）
NOTES = {
    1: ("免责声明", "开机首屏，黑底白字，底部提示按“确定”同意、按“返回”退出"),
    2: ("浏览主页", "标题“浏览”，2x3 图标，左上放大镜带选中框"),
    3: ("组数 1 / 芯片 56Bit", "频率 432、周期 103us，码 A9A092C3B03871"),
    4: ("设置菜单（亮度 100%）", "反显:关 / 使用说明 / 关于本设备 / 固件更新 / MAC BBCC92330C00"),
    5: ("新建主页", "标题“新建”，绿色文档图标带选中框"),
    6: ("组数 152 / 芯片 2262", "位置 0001、频率 433、周期 090us，底部“按发射键测试此码”"),
    7: ("新建主页", "同第 5 页，导航返回后的同一页"),
    8: ("组数 152 / 芯片 2262", "与第 6 页同页面，数值/高亮行不同"),
    9: ("新建主页", "同第 5 页"),
    10: ("组数 152 / 芯片 2262", "与第 6 页同页面，数值/高亮行不同"),
}


def main():
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)
    files = sorted(glob.glob(os.path.join(SRC, "page_*.png")))
    cards = []
    for i, f in enumerate(files, 1):
        name, desc = NOTES.get(i, ("页面 %d" % i, ""))
        dst = os.path.join(OUT, "%02d_%s.png" % (i, name.replace(" ", "").replace("/", "_")))
        shutil.copyfile(f, dst)
        cards.append((os.path.basename(dst), i, name, desc))

    rows = "\n".join(
        '<figure><a href="%s"><img src="%s"></a>'
        '<figcaption><b>#%d %s</b><br><small>%s</small></figcaption></figure>'
        % (n, n, i, nm, ds) for n, i, nm, ds in cards)

    html = """<!doctype html><meta charset="utf-8">
<title>单片机全部页面截图（%d 张）</title>
<style>
body{background:#15181c;color:#e6e6e6;font:14px/1.65 "Microsoft YaHei",sans-serif;margin:28px}
h1{font-size:19px;font-weight:600;margin:0 0 6px}
p.hint{color:#8b949e;margin:0 0 22px}
.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:20px}
figure{margin:0;background:#1e2228;border:1px solid #2c323a;border-radius:10px;padding:12px;text-align:center}
img{width:100%%;image-rendering:pixelated;border-radius:6px;background:#000}
figcaption{margin-top:10px;font-size:12.5px;color:#c9d1d9;line-height:1.55}
small{color:#7d8590}
</style>
<h1>单片机全部页面截图（%d 张，160x128）</h1>
<p class="hint">按抓包时间顺序排列。点图片看原尺寸。第 5/7/9 页是同一个"新建"主页，
第 6/8/10 页是同一个"组数 152"测试页（导航往返、数值/高亮行略有不同）。</p>
<div class="g">%s</div>
""" % (len(cards), len(cards), rows)

    p = os.path.join(OUT, "index.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(html)

    # 拼版（3 倍放大，方便一眼看全）
    ims = [Image.open(os.path.join(OUT, c[0])).convert("RGB") for c in cards]
    S = 3
    W, H = 160 * S, 128 * S
    cols = 5
    rows_n = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (W + 8) + 8, rows_n * (H + 20) + 8), (28, 28, 32))
    from PIL import ImageDraw
    for i, im in enumerate(ims):
        t = im.resize((W, H), Image.NEAREST)
        d = ImageDraw.Draw(t)
        d.rectangle([0, 0, 30, 14], fill=(0, 0, 0))
        d.text((3, 2), "%d" % (i + 1), fill=(255, 90, 90))
        sheet.paste(t, (8 + (i % cols) * (W + 8), 8 + (i // cols) * (H + 20)))
    sheet.save(os.path.join(OUT, "全部页面_拼版.png"))

    print("输出目录:", OUT)
    for c in cards:
        print("  ", c[0], "|", c[2])
    print("总览:", p)
    print("拼版:", os.path.join(OUT, "全部页面_拼版.png"))


if __name__ == "__main__":
    main()
