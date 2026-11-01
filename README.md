# 凭证归档 · Receipt Archive

把散落在手机里的**订单截图**和**支付截图**，整理成一份可以直接打印、提交的报销材料。

离线 OCR · 本机处理 · 自动配对 · 导出 Word / PDF

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB.svg)
![Platform](https://img.shields.io/badge/Platform-Ubuntu%20%7C%20Windows-4B5563.svg)
[![CI](https://github.com/ZzxArthur/receipt-archive/actions/workflows/ci.yml/badge.svg)](https://github.com/ZzxArthur/receipt-archive/actions/workflows/ci.yml)

[快速开始](#快速开始) · [使用方法](#使用方法) · [局限性](#局限性) · [English](README.en.md)

> 它处理的是**报销佐证材料**，不是增值税发票本身。数电票（PDF / OFD / XML）、发票查验、记账报税不在范围内。

---

## 它解决什么问题

报销时真正麻烦的往往不是填单，而是**把一笔业务的两张截图找齐**：

- 订单页在一张图里，支付凭证在另一张图里；
- 一次导入几十张，顺序是乱的；
- 同一张图被重复保存、重复发送；
- 一笔订单可能分两次、三次扣款；
- 最后还要把「订单在上、支付凭证在下」排成 Word 或 PDF。

「凭证归档」把这条链路压缩成三步：**导入 → 核对 → 导出**。所有识别、配对和排版都在本机完成。

## 功能

- **多种导入方式**：拖拽、文件选择、`Ctrl / ⌘ + V` 粘贴；可以分多次添加，持续累积在同一批次。
- **像素级去重**：内容完全相同的图片自动跳过，改名也不会重复；金额相同但画面不同的凭证会保留。
- **离线 OCR**：RapidOCRLite 原生 CPU 引擎 + PP-OCRv6 tiny 模型，随包附带，不联网、不需要 API Key。
- **字段提取**：区分「订单 / 支付 / 待确认」，提取实际支付金额、日期、时间、商户、订单号。
- **保守配对**：按金额、订单号、日期、商户、时间综合判断；支持一笔订单最多 4 张分笔支付；只有**唯一解**才自动确认，有歧义就交给人工。
- **人工兜底**：金额、类型、日期、商户都能改；可手动关联订单与支付截图；重新识别时保留手动修改。
- **A4 导出**：一笔订单独立分页，订单在上、支付凭证在下；可自动裁掉底部广告；导出 Word、PDF 或两者打包的 ZIP。
- **会话可恢复**：关闭窗口、升级程序不会丢数据；重新打开继续上次批次。

单批次上限：**500 张截图、单张 20 MB、合计 512 MB**。

## 快速开始

### 方式一：下载安装包

从 [Releases](https://github.com/ZzxArthur/receipt-archive/releases) 下载对应平台的包：

| 平台 | 文件 | 说明 |
| --- | --- | --- |
| Ubuntu 20.04 / 22.04 / 24.04 LTS (x86_64) | `receipt-archive_1.3.1_amd64.deb` | `sudo apt install ./receipt-archive_1.3.1_amd64.deb`，从应用菜单打开 |
| Windows 10 / 11 (x64) | `receipt-archive_1.3.1_windows_x64.zip` | 完整解压后双击 `run.vbs`；不要直接在压缩包里运行 |

Ubuntu 版需要 GTK 3 / WebKitGTK 2.36+（apt 会自动处理依赖）。Windows 版需要 Microsoft Visual C++ v14 x64 Runtime；系统没有时从[微软官方页面](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist)安装。

> 仓库不提交二进制安装包。Releases 里还没有对应包时，用下面的「方式二」从源码运行。

### 方式二：从源码运行

需要 Python 3.12、[uv](https://docs.astral.sh/uv/)，以及网络（仅首次下载依赖与模型）：

```bash
git clone https://github.com/ZzxArthur/receipt-archive.git
cd receipt-archive

uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --no-deps -r requirements-linux.lock
.venv/bin/python scripts/prepare_ocr_models.py

# 浏览器模式
.venv/bin/python -m receipt_tool.app --host 127.0.0.1 --port 8765
# 打开 http://127.0.0.1:8765

# 或者 Ubuntu 原生窗口（需要 GTK 3 / WebKitGTK）
./desktop.sh
```

也可以直接运行：

- `./start.sh`：自动创建 `.venv`、准备模型并前台启动；
- `./open.sh`：后台启动并打开浏览器。

### 方式三：构建安装包

```bash
# Ubuntu deb（需要 uv、dpkg-deb、desktop-file-validate）
python3 scripts/build_linux_package.py

# Windows 便携 ZIP（可在 Linux 上交叉构建，产物仍需 Windows 实机验证）
python3 scripts/build_windows_package.py
```

产物分别位于 `dist/ubuntu/` 和 `dist/windows/`，并附带 SHA-256 校验文件。

## 使用方法

### 1. 导入截图

打开应用后，把订单页、支付页截图拖进窗口，或点击选择文件、直接 `Ctrl / ⌘ + V` 粘贴。

- 订单和支付截图可以混在一起导入，顺序不影响配对；
- 可以分多次添加，截图会累积在当前批次；
- 内容完全相同的图片会被自动跳过，并在页面上提示。

### 2. 开始识别

添加完成后点击「开始识别」。程序会逐张做离线 OCR，判断截图类型，并提取金额、日期、商户、订单号。

识别过程完全在本机进行，不会上传图片。截图越多，耗时越长；识别期间可以继续查看已完成的结果。

### 3. 核对识别结果

切换到「识别结果」，重点看三件事：

1. **截图类型**：这张是订单，还是支付凭证？识别不确定时会标成「待确认」。
2. **实际金额**：以支付凭证上的「实际支付 / 实付 / 扣款金额」为准。
3. **日期和商户**：日期用于辅助配对，商户用于判断是不是同一笔。

金额、类型、日期、商户都可以直接修改，保存后重新自动配对即可。

### 4. 核对配对

切回「配对预览」。每一组应该是：

```text
订单截图
  └─ 支付截图（可以有多张）
```

- 绿色「已配对」表示金额和辅助信息都吻合；
- 黄色「需要核对」表示金额不一致、识别可信度低，或存在多种可能；
- 点「核对 / 调整配对」可以手动指定订单和支付截图；
- 勾选多张支付截图时，页面会实时显示合计金额和差额。

一笔订单最多支持 4 张分笔支付；只有金额合计完全一致且组合唯一时，才会自动确认。

### 5. 导出

选择格式：

- **Word + PDF**：打包成 ZIP，两种都保留；
- **Word 文档**：`.docx`；
- **PDF 文档**：`.pdf`。

可以填写文档标题，选择是否自动裁掉截图底部的广告区域。存在未配对或待核对项时，导出会被拦住；可以继续核对，或勾选「同时导出待确认与未配对截图」。

导出的版式是 A4、一笔订单独立分页、订单在上、支付凭证在下。

### 6. 开始下一批

当前版本一次只维护一个工作批次。确认本批已经导出后，点击「清空本批次」，再导入下一批。

> 「清空本批次」会删除当前批次的截图和识别结果。已经下载到电脑上的 Word / PDF 不受影响。

## 局限性

先说清楚边界，避免用错场景。

| 限制 | 具体表现 | 建议 |
| --- | --- | --- |
| **只针对中文订单 / 支付页面** | 内置滴滴、高德、美团打车、中国铁路等商户别名；其他语言、其他版式可能识别成「待确认」 | 手动填写类型和金额，或改 `recognition.py` 补规则 |
| **只处理截图** | 支持 PNG / JPEG / WEBP / TIFF / BMP；不读 PDF、OFD、XML 电子发票 | 电子发票先转成图片，或使用专门的发票工具 |
| **OCR 有准确率上限** | tiny 模型对低分辨率、强压缩、花哨背景、艺术字体的截图可能识别失败 | 尽量用原图；识别失败时手动补金额和类型 |
| **配对要求金额精确相等** | 差 1 分钱、部分退款、优惠后金额不一致、跨币种，都不会自动配对 | 手动配对；不要为了省事改金额 |
| **一笔支付只能归一个订单** | 购物车合并付款、一张支付截图对应多笔订单，当前不支持 | 拆成单笔支付截图，或分别整理 |
| **不读手机状态栏时间** | 截图上没有「支付时间 / 下单时间 / 行程结束」等标签时，日期可能为空 | 手动填写交易日期 |
| **一次只维护一个批次** | 清空当前批次后才能开始下一批；清空会删除当前批次数据 | 先导出，再清空；重要截图另存备份 |
| **单批容量有限** | 最多 500 张、单张 20 MB、合计 512 MB | 一个月 / 一个项目分一批 |
| **Windows 包未实机回归** | 安装包在 Linux 上交叉构建，尚未在 Windows 10/11 做完整回归 | 优先用源码运行，或先在测试机验证 |
| **OCR 引擎是预发布版本** | 固定使用 `RapidOCRLite==5.0.0a15`，升级时行为可能变化 | 升级前跑一遍自己的截图样本 |
| **没有移动端 / 远程访问** | HTTP 只监听 `127.0.0.1`，手机不能直接访问 | 在电脑上操作；需要时通过文件传输把截图送到电脑 |

### 明确不做

- **不做发票查验**。全国增值税发票查验平台需要图形验证码，没有面向个人的公开 API；自动绕过验证码既不稳定也不合规。
- **不解析数电票**。PDF / OFD / XML 电子发票不在范围内。
- **不做记账、申报、开票**。它是报销材料的整理器，不是财务系统。
- **不上传任何图片**。没有云端 OCR、没有遥测、没有账号体系。

## 常见问题

**为什么截图被标成「待确认」？**

常见原因：不是订单 / 支付页、截图太模糊、金额附近没有「实付 / 合计」等标签、检测到退款 / 失败 / 关闭状态。程序宁可让你手动确认，也不猜。

**为什么金额差 1 分钱也不自动配对？**

金额精确相等是这套工具最重要的安全线。差 1 分钱可能是手续费、优惠，也可能是另一笔交易；自动放宽会掩盖错配。请手动确认。

**截图会上传到网上吗？**

不会。OCR、配对、Word / PDF 生成全部在本机完成。唯一的网络行为是首次从源码运行时下载依赖和模型；安装包内置模型，运行时不需要网络。

**数据保存在哪里？怎么备份？**

| 平台 | 截图与识别数据 | 日志 |
| --- | --- | --- |
| Ubuntu | `~/.local/share/receipt-archive/data/` | `~/.local/state/receipt-archive/` |
| Windows | `%LOCALAPPDATA%\ReceiptArchive\data\` | 同目录 `backend.log` / `desktop.log` |

可用 `RECEIPT_DATA_DIR` 覆盖数据位置。备份时复制整个 `data/` 目录即可；换电脑后放回同一路径。

**卸载会删除我的截图吗？**

卸载程序通常只删除应用文件，不会主动删除用户数据目录。确认不再需要后，手动删除上面的 `data/` 目录。

**支持微信 / 支付宝的账单长截图吗？**

它更适合单笔交易的「订单详情」和「支付详情」页。一整张包含多笔交易的账单长图，目前只会被当成一张截图处理，无法自动拆成多笔。

**为什么导出按钮是灰的？**

还有待识别、未配对或待核对的截图。先完成识别和核对；如果确实要带着问题导出，勾选「同时导出待确认与未配对截图」。

**支持 macOS 吗？**

当前只提供 Ubuntu 和 Windows 的启动方式。macOS 可以尝试从源码运行浏览器模式，但没有官方安装包和测试。

## 工作原理

```text
图片 ──► 解码 / EXIF 校正 / 内容哈希去重
          │
          ▼
      PP-OCRv6 tiny 检测 + 方向分类 + 识别
          │
          ▼
  版面归一化（坐标转 0–1，按行排序）
          │
          ├─ 类型：订单 / 支付 / 待确认（关键词 + 版面）
          ├─ 金额：整数分；语义标签 + 空间位置 + 字号权重
          ├─ 日期 / 时间：只认交易或行程标签，不读手机状态栏
          └─ 商户 / 订单号：别名表 + 邻近值
          │
          ▼
  配对：金额精确相等 + 订单号 / 日期 / 商户 / 时间兼容
          └─ 支持 1–4 张分笔；只有唯一组合才自动确认
          │
          ▼
  DOCX（OpenXML）/ PDF 1.4：标准库 + Pillow 生成，一笔一页
```

几个有意的设计选择：

- **金额只用整数分**，匹配和合计都不经过浮点数。
- **识别可以失败，但不能猜**。低置信度、金额冲突、多个候选组合都会进入人工核对。
- **不读截图状态栏时间**。只使用带有「支付时间 / 下单时间 / 行程结束」等标签的时间，避免把截图时刻当成交易时刻。
- **退款、失败、关闭的订单不会被当成支出**，类型会落到「待确认」。
- **HTTP 服务只监听回环地址**，并校验 `Host` 头；没有远程接口。

## 开发与测试

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --no-deps -r requirements-linux.lock

.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q receipt_tool desktop windows scripts
```

测试不需要网络，也不读取真实截图：OCR 用例使用构造的文字框，导出用例使用临时生成的纯色图片。

## 项目结构

```text
receipt_tool/         标准库 HTTP 服务、离线 OCR、配对、Word/PDF 导出
  app.py              路由、上传校验、批次存储、导出接口
  recognition.py      OCR 结果解析、金额/日期/商户提取、配对算法
  exporting.py        纯标准库 + Pillow 生成 DOCX / PDF
  static/             无框架前端（HTML / CSS / JS）
  models/             固定版本 PP-OCRv6 tiny 模型、词典、许可证
desktop/              Ubuntu GTK / WebKitGTK 窗口、启动器、拖拽导入
windows/              Windows 便携启动器与 cmd/vbs 入口
scripts/              模型准备、双平台构建
tests/               标准库 unittest：解析、配对、导出、HTTP 接口
docs/                 背景调研与设计笔记
```

## 贡献

欢迎 Issue 和 Pull Request。开始之前请读 [CONTRIBUTING.md](CONTRIBUTING.md)，尤其是「默认离线、标准库优先、金额只用整数分、识别不能猜」这几条约束。

安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要公开提交。更新记录见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证与第三方

本项目代码采用 [MIT License](LICENSE)。

OCR 采用 [RapidAI/RapidOCRLite](https://github.com/RapidAI/RapidOCRLite) 的原生 CPU 实现；PP-OCR 权重源自 PaddleOCR，按 Apache-2.0 许可随包分发。模型目录中的 [LICENSE.txt](receipt_tool/models/LICENSE.txt)、[NOTICE.txt](receipt_tool/models/NOTICE.txt) 和 [MODELS.json](receipt_tool/models/MODELS.json) 记录了来源、版本与 SHA-256。

安装包还包含 Python 运行时及其依赖，各自的许可证保留在发行包的 `runtime/licenses/` 和 `*.dist-info` 目录中。
