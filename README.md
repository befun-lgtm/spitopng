# ST7735S 逻辑分析仪抓包 → 屏幕截图还原

从 PulseView / sigrok / Saleae Logic 2 导出的 SPI 波形里还原 ST7735S（160×128，RGB565）的屏幕内容。
单片机侧不用改代码，也不接屏——逻辑分析仪并联在 FPC 总线（SCL / SDA=MOSI / CSX / DCX + GND）上抓一段即可。

已经被用来完整导出过一台「学习码遥控器」的全部界面（12 页 + 11 页两轮，逐像素可读）。

## 0. 三步上手

```bash
pip install -r requirements.txt

# ① 先体检：确认通道角色 / 时钟沿对不对（10 秒）
python diag_channels.py digital.csv

# ② 出图：一条命令，把抓包里出现过的每个界面都还原成 PNG
python fast_decode.py digital.csv --out-dir pages
```

`pages/page_01.png …` 就是各个界面，原尺寸 160×128。抓包有多长都行，2000 万行大约 17 秒。

如果 `diag_channels.py` 报「命令分布不干净」，说明夹错线、采样率不够、或抓的不是这块屏 ——
先看 [§2f 采样率](#2f-采样率是硬门槛2026-11-02-踩了两小时定案) 和 [§6 排查顺序](#6-采不到图时按这个顺序排查)。

## 0b. 仓库结构

| 文件 | 作用 |
|---|---|
| **`fast_decode.py`** | **推荐入口**。跳变表 CSV → 所有页面 PNG，自动判时钟沿 |
| `diag_channels.py` | 通道角色 / 采样沿自检，夹错线时一次找出正确组合 |
| `st7735_decode.py` | 主解码器。单帧还原 + 全部参数 + `--auto` 自动侦测（`fast_decode` 内部就用它） |
| `l2cap.py` | 二进制位图通道（旧的缓冲路径，`csv2bins.py` 配套） |
| `csv2bins.py` | CSV → 每通道 `.bin`（只在要用 l2cap/snapshot_pages 时才需要） |
| `snapshot_pages.py` | 增量合成 + 按时间快照切页（`fast_decode` 复用了它的合成逻辑） |
| `saleae_pages.py` | Saleae Logic 2 gRPC 自动抓包 → 直接出页面（可选） |
| `page_watcher.py` | 老方案：连着 Logic 2 一边录一边切页 |
| `make_gallery.py` | 把页面目录生成 `index.html` 总览 |
| `make_test_csv.py` | 合成测试波形（没有硬件也能验证整条链） |
| `selftest.py` / `selftest_bin.py` / `selftest_v0.py` | 三套自测，不需要真硬件 |
| `开始截图.bat` | Windows 一键跑 `page_watcher.py` |


## 1. 依赖

```bash
pip install pillow        # 只这一个第三方包；不装也能跑，会退化成输出 PPM
```

Python 3.8+。

## 2. 不想管参数：加 `--auto`

```bash
python st7735_decode.py capture.csv --auto -o shot.png
```

它自己会：

| 项目 | 判据 |
|---|---|
| CS 极性 | 时钟边沿发生在哪个 CS 电平上（片选无效时时钟是静止的） |
| 采样沿 / SPI 模式 | 8 种组合各解一遍，按"命令可识别率"打分；错的沿解出来是乱码 |
| 位序 MSB/LSB | 同上（错的位序 `0x2A/0x2B/0x2C` 基本对不上） |
| DC 极性 | 同上；反了的话所有字节都变命令，"命令占比过高"直接扣分 |
| CPOL | CS 无效期间 SCL 停的电平 |
| offset_x / offset_y | 抓到 `0x2A/0x2B` 后，窗口宽度 == 面板宽度时，起点就是偏移 |
| RGB565 字节序 | 两种拼法各出一张图，比相邻像素差的均值，小的那个对（错序会把亮度噪声搬进色度） |
| 方向/镜像/颜色 | 从 `0x36 MADCTL` 的 MV/MX/MY/bit3 推 |

判定过程会完整打在输出里（每项打分 + 为什么选它），出错时能一眼看出卡在哪一步。
探测的采样点不够（比如抓包开头只有初始化、还没到 `0x2C`）会自动放大 4 倍重试。

## 2b. PulseView 的 8 通道 `digital.csv` 直接拖进来

PulseView「Export → CSV」默认导出的是**边沿表**，长这样：

```
Time [s],Channel 0,Channel 1,Channel 2,Channel 3,Channel 4,Channel 5,Channel 6,Channel 7
0.000000000,1,1,1,1,1,1,1,1
2.971398375,1,1,1,0,1,1,1,1
```

这种文件不需要改列名、不需要转格式，直接用：

```bash
python st7735_decode.py digital.csv --auto -o shot.png
```

程序会自己认通道：

| 角色 | 判据 |
|---|---|
| SCL | 翻转次数最多的那一路（时钟一定最快） |
| SDA/MOSI | 翻转次数第二多的 |
| CS | 剩下的活动通道里，**时钟边沿只落在它有效那一侧**的那一路（这条最硬：片选无效时时钟不动） |
| DC | 再剩下的那一路 |

认完会打印一张「通道角色」表，翻车就用手工指定（填通道号）：

```bash
python st7735_decode.py digital.csv --auto -o shot.png \
    --scl-col 3 --sda-col 1 --cs-col 2 --dc-col 4
```

> **只要 2 根线是救不回来的。** 缺 CS 还能当"一直有效"硬解，缺 DC 就彻底没办法——
> 分不出命令字节和像素字节，永远找不到 `0x2C`。程序遇到这种情况会直接打印体检结论，
> 不会假装出了张图。

### 2c. 先看「通道体检」：确认夹子有没有夹在总线上

任何一份 PV 抓包，程序都会先打一张体检表再解码。它回答一个问题：
**这堆线上到底有没有 SPI 总线？** 判据三条：

| 门槛 | 判据 | 为什么 |
|---|---|---|
| 去重 | 两路边沿 ≥90% 重合 → 判为同一根线，不参与配对 | 夹子太宽压住两个焊盘时会这样，白废一个通道 |
| 时钟资格 | 占空比 15~85% | 硬件 SPI 是近 50%，软件位翻转天然不对称，但不会到 8% 那种尖峰 |
| 相位对齐 | MOSI 边沿落在时钟周期的同一相位，集中度 >75% | 同一颗 MCU 驱动的两根线，边沿必然锁相 |

只看第 3 条会误判：开关电源的基波和它的谐波之间集中度也是 100%（占空比会露馅）。

没通过时程序会直接打印每个夹子实际夹到了什么，例如：

```
Ch0  620235  1.12us  563ns   67%     7%   周期约 1.69us，占空比 67%，周期稳定性 7%
Ch3   41097  299us   199us   60%   100%   低频方波 2.01 kHz，占空比 60% → 背光 / 电源 PWM
Ch4  337093  437ns   5.19us   8%    78%   窄脉冲 437ns 宽、每 5.19us 一个 → 开关电源 / 耦合尖峰
```

看到"背光 PWM""开关电源"就说明夹在电源脚上了，不是数据线。

### 2d. 14 针 FPC 怎么找准 5 根线

**这块板子的引脚是确定的**（从重建固件 `app/ui_pages.c` 读出，不是猜的）：

| 信号 | MCU 引脚 | 排线针脚 | 说明 |
|---|---|---|---|
| SCL | **PA6** | 9 | 软件位翻转，实测 1.776MHz |
| SDA | **PA7** | 8 | 每位一次，MSB 先 |
| CS  | **PC2** | 12 | 低有效，每个字节事务拉低 |
| DC  | **PC4** | 7 | 高 = 数据，低 = 命令 |
| RESX| **PC5** | 6 | 上电时拉低一个脉冲 |

驱动方式是 `ui_lcd_shift_byte()` 里的 GPIO 位翻转：`PA7=bit → PA6 拉低 → PA6 拉高`。
所以**时钟占空比不对称、周期有抖动都是正常的**，不要当成"信号不对"。
一帧 = 160x128 RGB565 = 40960 字节，每次写 320 字节/行 × 128 行。

> **⚠ 别用测试钩夹排线。** 连接器 `AFC11-S14ICC-00` 是 **0.5mm 间距**，14 针总宽只有 6.5mm，
> 而测试钩约 2mm 宽 → 必然压住 3~4 针。相邻的 SDA(8) 和 SCL(9) 会被直接短路，
> 抓出来是两路脉冲完全重合的废数据（程序会提示"Channel X 和 Channel Y 是同一根线"）。
>
> 三个可行改法：
> 1. **夹 MCU 引脚**（TQFP64，0.8mm 间距，引脚在封装外缘，细针/缝衣针可单点接触）
> 2. **焊 0.1mm 漆包线**到焊盘，再接到分析仪
> 3. 用 **0.5mm 14P FPC 测试转接板**

如果拿不到引脚图，也可以用万用表反推（排线定义
`1 NC / 2 GND / 3 LED-K / 4 LED-A / 5 GND / 6 RESX / 7 DCX / 8 SDA / 9 SCL / 10 VDD / 11 VDD1 / 12 CSX / 13 GND / 14 NC`）：
GND 只有 3 根，5 号和 13 号之间正好夹着 7 针（6~12），其中 10/11 量得到 3.3V。

> **静态画面不发像素流。** 屏幕不动的时候总线上几乎没有数据（实测只有几十字节/秒），
> 自然抓不到 `0x2A/0x2B/0x2C`。抓之前必须先制造整屏刷新：断电重上电、切页面、按菜单，
> 然后连续抓 ≥5 秒，采样率 ≥8MHz。

### 2e. 这块屏的实测定案参数（抓成功的那一份）

夹对线之后跑 `--auto` 一次命中，全程零警告。**下次同款板子直接用这组参数**：

| 项 | 值 | 怎么确认的 |
|---|---|---|
| SPI 时序 | **Mode 3**（CPOL=1 CPHA=1），上升沿采样，MSB first | 8 组候选里只有它命令可识别 100% |
| CS / DC | **低有效** / **低 = 命令** | 3,095,887 个时钟边沿落在 CS=0，仅 3 个在 CS=1 |
| 通道角色 | **Ch0=SCL、Ch3=SDA、Ch1=CS、Ch2=DC** | Ch0 翻转 309 万次即 1.776MHz 时钟 |
| 时钟实测 | **1.776 MHz** | 位翻转，不是硬件 SPI |
| 面板 / 偏移 | **160×128，offset = (1, 2)** | 整屏 CASET=1..160、RASET=2..129 |
| 字节序 | **高字节先**（大端） | 大端平滑度 16.8 / 小端 52.2 |
| 颜色顺序 | **RGB**（MADCTL D3=0，不交换） | 图标语义色（绿/紫/黄）都对 |

两个容易踩的坑，已经做进程序：

1. **`MADCTL=0xA0`（MY|MV）不要当成几何变换去做。** 它确实带 MV 位，但整屏窗口的
   x 跨度仍然等于面板宽（160），说明 MV 对**逻辑地址**没生效——应用写进 GRAM 的就是最终
   逻辑帧，控制器只管扫描方向。照 MADCTL 去转 90° 只会把图拧歪。程序现在用
   "窗口跨度 vs 面板尺寸" 的实测证据来判定，不信声明。
2. **`CS` 是"每个 16 位字拉一次"**，所以 193,493 字节对应 105,034 个事务，平均 1.84 字节/事务。
   这跟固件里 `ui_lcd_write_word16()` 的行为一致。**看到事务又碎又多不是接线错**，
   解析器本来就跨事务累积状态。

### 2f. ★★★ 采样率是硬门槛（2026-11-02 踩了两小时定案）

**这块屏的 SCL 是软件位翻转，实测 1.776 MHz（周期 563 ns）。逻辑分析仪采样率必须
≥ 16 MHz（推荐 24 MHz）；只给 4 MHz 时，一个时钟周期只有 2.25 个采样点，
**比时钟窄的脉冲会被整根漏掉**，字节边界就乱了。

4 MHz 抓包的症状（一条不落都出现过，认准这个组合就是采样率不够）：

| 症状 | 说明 |
|---|---|
| 三条信号线跳变位置对 8 取模**均匀分布** | 真 DC 只能在字节边界跳变，应该集中在一个余数上 |
| 每字节应有 8 个时钟上升沿，实测 CS 低区间内是 **3~9 混着来**，只有 22% 是 8 | 约 78% 的字节丢位 |
| 命令种类 240+ 种（ST7735S 一共才二十几个），`0xFE/0xFD/0xFA` 高频出现 | 像素数据被误判成命令 |
| `0x2A/0x2B` 窗口偶尔还能解对（说明接线/通道角色没错），但每帧 `pix` 只有 1 | 命令段能勉强解，像素段全乱 |

**7.8 MHz 也不行**（换了 7.8 MHz 抓包仍然只有 2.25→4.4 个采样点/周期，勉强）。实测定案：
**24 MHz、只开 CH0~CH3、抓 20~30 s**，一次成功。

24 MHz 过关后的铁证 —— 整段 85 s 抓包里命令分布是：

```
命令分布: [(0x2A, 58773), (0x2B, 58773), (0x2C, 58771), 其余各 2 次(上电毛刺)]
```

只有三种命令，数量完全对齐，这才是对的。

### 2g. 从一堆局部刷新里挑「画完的界面」

这块板刷屏极碎：85 s 里 58,771 个 CASET/RASET/RAMWR 窗口，平均每窗口只有 ~5 个像素，
**只有 8 个窗口是整屏重绘**。所以不能只认整屏窗口切页，否则会漏掉大部分界面。

现成脚本：

```bash
python csv2bins.py digital.csv --out-dir _conv2 --pages pages_new   # CSV → 字节流 → 整屏页
python _pages_final.py _conv2 pages_final2                          # 平台期法提取每个界面
```

`_pages_final.py` 的判据：每 20 帧取一张增量合成快照 → 相邻快照差异 <0.10% 的连续段
视为「画面没动」→ 取该段**最后一张**（画完的状态）→ 段长 ≥8 张才认 → 再按 0.6% 去重。
调 `STRIDE / PLATEAU / MIN_RUN / DEDUP` 四个常量即可。

## 2h. `fast_decode.py`：一条命令把整段抓包拆成所有页面

```bash
python fast_decode.py digital.csv --out-dir pages
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--out-dir` | `pages_fast` | 输出目录 |
| `--channels SCL CS DC SDA` | `0 1 2 3` | CSV 里的数据列序号（= Logic 上的 Channel N）。**接线换了顺序就改这里**；5 列的 sigrok 导出常见是 `0 2 3 1` |
| `--width/--height` | `160/128` | 面板分辨率 |
| `--offset-x/--offset-y` | `1/2` | GRAM 偏移。默认值是那台遥控器实测的，**换一块屏一般要改成 `0 0`** |
| `--madctl` | `none` | `none` 不做几何变换；`auto` 用抓到的 `0x36` 推 MV/MX/MY |
| `--byte-order` | `big` | RGB565 高低字节顺序，反了会出现噪点/偏色 |
| `--color-order` | `rgb` | `auto` 按 MADCTL bit3 判 RGB/BGR |
| `--stats-only` | – | 只打印统计（看通道接没接对），不出图 |
| `--stride` | `20` | 每多少帧合成一张快照（越小越细、越慢） |
| `--plateau` | `0.10` | 相邻快照差异 < 该百分比 → 视为画面没动 |
| `--min-run` | `8` | 平台至少连续几张才算「画完」 |
| `--dedup` | `0.6` | 两个界面差异 < 该百分比 → 视为同一页 |
| `--rising` / `--falling` | 自动 | 强制采样沿，一般不用给 |

> 默认值是按「160×128 + 偏移 (1,2)」调好的，换设备也能用，但要改 `--offset-x/--offset-y`。
> 不确定就先用 `st7735_decode.py --auto` 把参数都探一遍，再拿同样参数喂给 `fast_decode.py`。

它做的事和慢速路径完全一样，只是把两个浪费掉的步骤砍了：

1. **不再把采样点展开成位数组**。Logic 导出的 CSV 是**跳变表**，每行 = 该时刻 8 路电平；
   在「时钟沿那一行」直接读 CS/DC/SDA 的列值就是采样结果，零解包。
2. **不再逐行用 `csv` 模块解析**。改 `pandas.read_csv(usecols=...)`，2200 万行 5 秒。

| 步骤 | 旧链路 | `fast_decode.py` |
|---|---|---|
| 读 22.4 M 行 CSV | ~40 s | 2.5 s |
| 成字节流（1.18 M 字节） | ~20 s | 0.6 s |
| 解帧 + 合成快照 2183 张 | ~15 s | 1.6 s |
| **合计** | **几分钟** | **17 s** |

### 时钟沿会自动判，不用手选

同一条线两次抓包，一次是**上升沿**干净、一次是**下降沿**干净。所以脚本默认两个都试，
按「`0x2A/0x2B/0x2C` 命中数多、命令种类少」自动选，并打印：

```
    上升沿: 字节 475823 命令种类 23 2A/2B/2C=2451
    下降沿: 字节 475823 命令种类 22 2A/2B/2C=35876
[3.4s] 采用下降沿  字节流 475823
```

判错时的典型症状：主峰命令是 `0x95`（正好是 `0x2A` 按位取反）、命令 240 多种。

## 3. 最短用法（参数已知时）

```bash
python st7735_decode.py capture.csv -o shot.png
```

默认值就是题目给的条件：`time,scl,sda,csx,dcx` 五列、Mode 0、MSB first、CS 低有效、
DC 低=命令、160x128、RGB565 高字节先、offset=(0,0)、MADCTL 自动。

## 4. 完整参数

```bash
python st7735_decode.py capture.csv \
    --sample-rate 24e6 \
    --spi-mode 0 --bit-order msb \
    --cs-active low --dc-command low \
    --width 160 --height 128 --offset-x 0 --offset-y 0 \
    --byte-order big --madctl auto --color-order auto \
    --render composite -o shot.png -v
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--sample-rate` | `24e6` | 逻辑分析仪采样率，只用于**反推 SPI 时钟**和判断 4 倍关系；支持 `24e6` / `24M` / `24000000` |
| `--format` | `auto` | `csv` / `vcd`；按扩展名自动判断 |
| `--header` | `auto` | 表头有无：`auto` 时第一行出现非数字 token 就当表头 |
| `--columns` | – | 按位置一次给全：`0,1,2,3,4`（time,scl,sda,cs,dc） |
| `--scl-col/--sda-col/--cs-col/--dc-col/--time-col` | – | 按**列名**取值（支持 `SCLK`/`MOSI`/`CS_N`/`A0` 等别名，也能写列号） |
| `--spi-mode` | `0` | 0/1/2/3；采样沿 = 上升 if CPOL==CPHA else 下降 |
| `--cpol/--cpha` | – | 单独覆盖 mode |
| `--bit-order` | `msb` | `msb` / `lsb` |
| `--cs-active` | `low` | 片选有效电平 |
| `--dc-command` | `low` | DC 为命令时的电平 |
| `--width/--height` | `160/128` | 面板分辨率（= 输出图像尺寸） |
| `--offset-x/--offset-y` | `0/0` | 控制器 GRAM 偏移，见第 6 节 |
| `--byte-order` | `big` | RGB565 高低字节顺序 |
| `--madctl` | `auto` | `auto` 用抓到的 0x36 做 MV/MX/MY 变换；`none` 不做几何变换；也可强制 `0xC8` |
| `--color-order` | `auto` | `auto` 按 MADCTL bit3 判断 RGB/BGR；可强制 `rgb`/`bgr` |
| `--render` | `composite` | 多帧时：`composite` 依次叠加 / `last` / `first` / `all`（逐帧出图） |
| `--bg` | `000000` | 未写入区域的背景色 |
| `--rotate` | `0` | 输出图顺时针旋转 90/180/270（需 Pillow） |
| `-v` | – | 打印命令直方图 + 关键命令序列 |

## 5. 输出统计

```
CS 事务数       : 13
SPI 字节数      : 40980（命令 10 / 数据 40970）
命令数(DC=0)    : 10
RAMWR(0x2C)次数 : 1
像素字节数      : 40960  =>  20480 个像素
跨 CS 事务      : 1 帧被拆到多个事务，共多用 2 个事务（涉及 3 个）
SPI 时钟(实测)  : 6.000 MHz（每位 4.00 个采样点，采样率 24.00 MHz）
```

警告会针对这些情况给出：SPI > 6MHz / 采样率不足 4 倍 / 像素不足一帧 / 窗口写不满 /
像素越界被裁（offset 或 MADCTL 不对）/ 没抓到 0x2A/0x2B / CS 撤销时有半字节 /
COLMOD 不是 16bit / 抓包里没有 0x36。

## 6. 采不到图时按这个顺序排查

0. **先看「通道角色」表里有没有 `(全程无变化)`** —— 有的话就是那根线没接/没夹好，
   这时候任何参数都救不回来，直接补抓。**必须夹够 4 根：SCL、MOSI、CS、DC**（外加 GND）。
1. **确认 CS 真的在动**：`CS 事务数` 应该是几十~几百。若是 1，说明 CS 一直低（或者抓错线）。
2. **看 `SPI 字节数` 和 `像素字节数`**：字节数对不上 → SPI 模式/位序/采样率问题；
   字节数正常但没有像素 → DC 极性反了（试 `--dc-command high`）。
3. **图是乱码/斜条纹** → 十有八九是 SPI 时钟 > 采样率/4，降频重抓。
4. **图整体偏移、右边或下边缺一条** → `--offset-x/--offset-y` 不对（见下）。
5. **图上下颠倒/左右镜像** → `--madctl none` 对比一下，确认是不是把 MADCTL 的镜像重复应用了。
6. **颜色红蓝反了** → `--color-order bgr`（或反过来 `rgb`）。
7. **看「时间网格 ns → 等效采样率」那行**：如果显示 8MHz 而你设的是 24MHz，
   说明这台分析仪插满 8 通道后每通道只有 8MHz —— 那 SPI 时钟就必须压到 2MHz 以下，
   否则边沿会漏采，解出来全是错字节。

## 7. offset 到底填多少

ST7735S 的 GRAM 是 **132x162**，小屏只用了其中一块：

| 常见面板 | 驱动里的 CASET/RASET | 建议 offset |
|---|---|---|
| 128x160 竖屏（1.8"） | 0..127 / 0..159 | (0, 0)，部分模组 (2, 1) |
| 160x128 横屏（MV=1） | 0..127 / 0..159 | 视模组，(1, 2) 之类 |
| 0.96" 80x160 | 24..103 / 0..159 | (24, 0) |

脚本做的事是 `图像坐标 = CASET/RASET 值 - offset`。
所以：**如果抓到的窗口起点不是 0（比如 2 / 1），offset 就必须填这个起点**，否则整幅图会平移。
不确定就先跑一遍，看图是往哪个方向偏、偏几个像素，再回来填。

## 8. 全自动抓包（Saleae Logic 2 自动化）

`page_watcher.py`（走 CSV，慢）已被 `saleae_pages.py` 取代：直接通过 Logic 2 的
自动化接口抓包，**你只管按设备上的按键，新页面自动存成 PNG**。

```bash
python saleae_pages.py --seconds 20 --rate 8000000
python saleae_pages.py --list          # 只列设备
```

输出到 `pages_auto/page_001.png` …，画面没变不会重复存。

### 8a. 这条链路的四个坑（都踩过，别再踩）

| 坑 | 现象 | 正解 |
|---|---|---|
| 缓冲太小 | `Capture stopped because the software memory buffer limit was reached`，连 1.2MB 数据都报满 | `buffer_size_megabytes=3072`。UI 里那个 0.03GB 是残留值，会误导 |
| 残留 session | 之前所有 capture 标签页占着软件内存，新抓取数据为空 | 重启 Logic 2，每次 capture 用完立刻 `close()` |
| 导出是**非阻塞**的 | `export_raw_data_binary` 返回成功但文件只有 44 字节 | 轮询文件大小，连续 3 次不变才算写完 |
| 不用 `with` 块 | 官方明说会有未定义行为，导不出数据 | `with automation.Manager.connect(port=10430) as m:` |

### 8b. 二进制导出不是位图，是**跳变列表**

Logic 8 导出的是 version 0 格式，这点很关键——它让数据量小了几个数量级：

```
8 字节  "<SALEAE>"
int32   version = 0
int32   type    = 0 (digital)
uint32  initial_state
double  begin_time
double  end_time
uint64  num_transitions
double  transition_time × num_transitions
```

所以 10 秒 8 通道的抓取，即使 SCL 有几百万个跳变，文件也只有几 MB；
解码用 `np.searchsorted` 在每个时钟沿上取其它通道电平，**整段 95 万字节只要 5 秒**。

### 8c. 接线自检

抓一次 8 通道就能看出线接在哪：

```bash
python saleae_pages.py --seconds 10 --rate 8000000   # 若报无数据，先做下面的自检
```

| 读数 | 含义 |
|---|---|
| 跳变数 0、初始电平 1 | 该通道**悬空**（没接线） |
| 跳变数 0、初始电平 0 | 接在 GND，或信号恒定低 |
| 每秒几十次跳变 | 可能只接了一根线，或屏幕静止 |
| 每秒几万次跳变 | 正常（SCL 或 SDA） |

期望的接线：**SCL→ch0、CS→ch1、DC→ch2、SDA→ch3**，外加一根 GND 共地。

## 9. 自测（不需要真硬件）

```bash
# 生成一份 6MHz SPI / 24MHz 采样的抓包，像素被拆成 3 个 CS 事务
python make_test_csv.py demo --spi-hz 6e6 --split 3

# 还原，和 demo/expected.png 应为逐像素一致
python st7735_decode.py demo/capture.csv -o demo/shot.png

# Saleae 自动化链路（v0 跳变列表解码）自测
python selftest_v0.py

# 其它组合
python make_test_csv.py demo2 --spi-mode 3 --cs-active high --dc-command high
python make_test_csv.py demo3 --offset-x 2 --offset-y 1 --partial "30,40,24,16,FF0000"
```

已实测通过：Mode 0/1/2/3、CS/DC 极性反相、offset (2,1)、MADCTL 0x00/0x08/0xC8、
RGB565 大端/小端、CS 拆分 1/2/3/5 段、VCD 输入、列顺序打乱+改名、截断抓包（输出部分帧并报警）。

`--auto` 的极限测试：故意用一套"全反"参数生成抓包
（`--spi-mode 3 --cs-active high --dc-command high --offset-x 2 --offset-y 1
--madctl 0xC8 --byte-order little --split 4`），命令行只给 `--auto`，
结果与期望图 **逐像素一致**（含 180 度镜像和 BGR/RGB 判断）。
`demo_auto_result.png` 就是这一次的输出。

一条命令跑完整个矩阵（14 组，全自动生成→解码→比对）：

```bash
python selftest.py
```

### `fast_decode.py` 的端到端自测

它和主解码器走的是两条不同的字节流实现，所以单独立了一个用例：

```bash
python make_test_csv.py demo --spi-hz 6e6 --split 3
python fast_decode.py demo/capture.csv --channels 0 2 3 1 \
       --offset-x 0 --offset-y 0 --color-order auto --out-dir demo/out
# 和 demo/expected.png 比对：0 / 20480 像素不一致

python fast_decode.py demo/capture.csv --channels 0 2 3 1 \
       --offset-x 0 --offset-y 0 --color-order auto --stats-only
# 应看到：命令 0x11 / 0x3A / 0x36 / 0x21 / 0x13 / 0x29 / 0x2A / 0x2B / 0x2C
```

```
默认(mode0/BGR/拆1)       通过（0 误差）
mode3                  通过（0 误差）
CS 高有效                 通过（0 误差）
MADCTL 0x00            通过（0 误差）
局部刷新(补丁区)              通过（补丁区 960 像素精确命中）
极限组合                   通过（0 误差）
通过 14 / 失败 0
```
