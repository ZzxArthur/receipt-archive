# 凭证归档 · Receipt Archive

把散落在手机里的**订单截图**和**支付截图**，整理成一份可以直接交付的报销材料。

离线 OCR · 本机处理 · 自动配对 · 一键导出 Word / PDF

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB.svg)
![Platform](https://img.shields.io/badge/Platform-Ubuntu%20%7C%20Windows-4B5563.svg)

---

## 它解决什么问题

报销时真正麻烦的往往不是填单，而是**把一笔业务的两张截图找齐**：

- 订单页在一张图里，支付凭证在另一张图里；
- 一次导入几十张，顺序是乱的；
- 同一张图被重复保存、重复发送；
- 一笔订单可能分两次、三次扣款；
- 最后还要把「订单一页、支付凭证在下」排成 Word 或 PDF。

「凭证归档」把这条链路压缩成三步：**导入 → 核对 → 导出**。所有识别和排版都在本机完成。

> 它处理的是**报销佐证材料**，不是增值税发票本身。数电票（PDF / OFD / XML）、发票查验和报销单目前不在范围内，见[路线图](#路线图)。

## 功能

- **多种导入方式**：拖拽、文件选择、`Ctrl / ⌘ + V` 粘贴；可以分多次添加，持续累积在同一批次。
- **像素级去重**：内容完全相同的图片自动跳过，改名也不会重复；金额相同但画面不同的凭证会保留。
- **离线 OCR**：RapidOCRLite 原生 CPU 引擎 + PP-OCRv6 tiny 模型，随包附带，不联网、不需要 API Key。
- **字段提取**：区分「订单 / 支付 / 待确认」，提取实际支付金额、日期、时间、商户、订单号。
- **保守配对**：按金额、订单号、日期、商户、时间综合判断；支持一笔订单最多 4 张分笔支付；只有**唯一解**才自动确认，有歧义就交给人工。
- **人工兜底**：金额、类型、日期、商户都能改；可手动关联订单与支付截图；重新识别时保留手动修改。
- **A4 导出**：一笔订单独立分页，订单在上、支付凭证在下；可自动裁掉底部广告；导出 Word、PDF 或两者打包的 ZIP。
- **会话可恢复**：关闭窗口、升级程序不会丢数据；重新打开继续上次批次。

## 不做什么

明确边界，避免误解：

- **不做发票查验**。全国增值税发票查验平台需要图形验证码，且没有面向个人的公开 API；自动绕过验证码既不稳定也不合规。
- **不解析数电票**。目前只处理截图（PNG / JPEG / WEBP / TIFF / BMP），不读取 PDF、OFD、XML 电子发票。
- **不做记账、申报、开票**。它是报销材料的整理器，不是财务系统。
- **不上传任何图片**。没有云端 OCR、没有遥测、没有账号体系。

## 快速开始

### 下载安装包

从 [Releases](https://github.com/ZzxArthur/receipt-archive/releases) 下载对应平台的包：

| 平台 | 文件 | 说明 |
| --- | --- | --- |
| Ubuntu 20.04 / 22.04 / 24.04 LTS (x86_64) | `凭证归档.deb` | `sudo apt install ./凭证归档.deb`，从应用菜单打开 |
| Windows 10 / 11 (x64) | `凭证归档.zip` | 完整解压后双击 `run.vbs`；不要直接在压缩包里运行 |

Ubuntu 版需要系统已安装 GTK 3 / WebKitGTK 2.36+（apt 会自动处理依赖）。Windows 版需要 Microsoft Visual C++ v14 x64 Runtime；系统没有时从 [微软官方页面](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist) 安装。

> 安装包由脚本构建，仓库不提交二进制文件。没有现成包时，按下面的「从源码运行」或「构建安装包」操作。

### 从源码运行

需要 Python 3.12、[uv](https://docs.astral.sh/uv/) 和网络（仅首次下载依赖与模型）：

```bash
git clone https://github.com/ZzxArthur/receipt-archive.git
cd receipt-archive

uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --no-deps -r requirements-linux.lock
.venv/bin/python scripts/prepare_ocr_models.py

# 浏览器模式
.venv/bin/python -m receipt_tool.app --host 127.0.0.1 --port 8765
# 打开 http://127.0.0.1:8765

# 或者使用 Ubuntu 原生窗口（需要 GTK 3 / WebKitGTK）
./desktop.sh
```

也可以直接运行 `./start.sh`（自动创建 `.venv`、准备模型并前台启动）或 `./open.sh`（后台启动并打开浏览器）。

### 构建安装包

```bash
# Ubuntu deb（需要 uv、dpkg-deb、desktop-file-validate）
python3 scripts/build_linux_package.py

# Windows 便携 ZIP（可在 Linux 上交叉构建，产物仍需 Windows 实机验证）
python3 scripts/build_windows_package.py
```

产物分别位于 `dist/ubuntu/` 和 `dist/windows/`，并附带 SHA-256 校验文件。

## 使用流程

1. **导入**：把订单页、支付页截图拖进来，或直接粘贴；可以分多次添加。
2. **识别**：点击「开始识别」。程序会逐张 OCR，区分类型并提取字段。
3. **核对**：在「识别结果」里修正金额、类型、日期、商户；有歧义的配对会标成「待核对」。
4. **配对**：在「配对预览」里确认「订单 → 支付」；分笔支付会核对扣款合计。
5. **导出**：选择 Word、PDF 或两者，填写可选标题，导出 A4 文档。

存在未配对或待核对项时，导出会被拦住；可以继续核对，或勾选「同时导出待确认与未配对截图」。

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

## 数据与隐私

| 平台 | 截图与识别数据 | 日志 |
| --- | --- | --- |
| Ubuntu | `~/.local/share/receipt-archive/data/` | `~/.local/state/receipt-archive/` |
| Windows | `%LOCALAPPDATA%\ReceiptArchive\data\` | 同目录 `backend.log` / `desktop.log` |

可用 `RECEIPT_DATA_DIR` 覆盖数据位置。只有点击「清空本批次」才会删除当前批次；关闭窗口和升级不会删除数据。

OCR、配对、Word 和 PDF 全部在本机完成。唯一的网络行为是首次从源码运行时下载依赖与固定版本的 OCR 模型；安装包内置模型，运行时不需要网络。

## 已知限制

- **只针对中文订单/支付页面**。内置滴滴、高德、美团打车、中国铁路等商户别名；其他版式可能需要补规则。
- **tiny 模型有准确率上限**。低分辨率、强压缩、花哨背景、艺术字体的截图可能识别失败，需要手动填写。
- **只做截图**。不读取 PDF / OFD / XML 电子发票，不做发票查验、查重台账、报销单、进项税抵扣。
- **配对要求金额精确相等**。不支持部分退款、优惠后金额不一致、跨币种；这类情况请手动配对。
- **Windows 包在 Linux 上交叉构建**，尚未在 Windows 10/11 实机完成全量回归。
- **OCR 引擎使用上游预发布版本** `RapidOCRLite==5.0.0a15`；升级时需重新跑回归。

## 路线图

以下是基于公开资料和真实报销流程整理的候选方向，**尚未排期**，欢迎在 Issue 里讨论优先级。详细调研见 [docs/invoice-research-2026-10.md](docs/invoice-research-2026-10.md)。

| 优先级 | 方向 | 解决什么 | 难度 |
| --- | --- | --- | --- |
| P0 | 多格式导入 + 文件判型 + 哈希去重 + 本地台账 | 发票散落、重复提交 | 低 |
| P0 | 数电票 XML 解析 + 价税勾稽 | 分不清 PDF / OFD / XML 哪个是原件 | 中 |
| P0 | 全局查重（号码 + 日期 + 金额 + 销方税号） | 同一张票重复报销 | 低 |
| P1 | OFD 解析 / 内嵌 XML / 转图 | 12306 等场景只有 OFD | 中 |
| P1 | 抬头库 + 税号校验 + 购买方比对 | 抬头、税号填错后红冲 | 低 |
| P1 | 发票 ↔ 订单 / 支付 / 行程单配对 | 金额、行程、抬头对不上 | 中 |
| P1 | 报销单生成（模板、金额大写、附件张数） | 填单和贴票 | 中 |
| P2 | 合并打印、归档包（原件 + 元数据 + 校验和） | 打印、长期归档 | 中 |
| P2 | 查验辅助（清单、人工查验、结果回填） | 查验要逐张过验证码 | 低（联网） |

**明确不做**：破解验证码、RPA 登录税局、自动开票、代账申报。这些需要企业资质或违反平台规则，也与「本地离线」的定位冲突。

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
scripts/              模型准备、双平台构建、用户级安装
tests/               标准库 unittest：解析、配对、导出、HTTP 接口
docs/                 调研笔记
```

## 开发与测试

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --no-deps -r requirements-linux.lock

.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q receipt_tool desktop windows scripts
```

测试不需要网络，也不读取真实截图：OCR 用例使用构造的文字框，导出用例使用临时生成的纯色图片。

## 贡献

欢迎 Issue 和 Pull Request。开始之前请读 [CONTRIBUTING.md](CONTRIBUTING.md)，尤其是「默认离线、标准库优先、金额只用整数分、识别不能猜」这几条约束。

安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要公开提交。

## 许可证与第三方

本项目代码采用 [MIT License](LICENSE)。

OCR 采用 [RapidAI/RapidOCRLite](https://github.com/RapidAI/RapidOCRLite) 的原生 CPU 实现；PP-OCR 权重源自 PaddleOCR，按 Apache-2.0 许可随包分发。模型目录中的 [LICENSE.txt](receipt_tool/models/LICENSE.txt)、[NOTICE.txt](receipt_tool/models/NOTICE.txt) 和 [MODELS.json](receipt_tool/models/MODELS.json) 记录了来源、版本与 SHA-256。

安装包还包含 Python 运行时及其依赖，各自的许可证保留在发行包的 `runtime/licenses/` 和 `*.dist-info` 目录中。
