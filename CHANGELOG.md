# 更新日志

本项目从 1.3.1 起采用公开的更新日志。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- 开源项目配套：MIT 许可证、贡献指南、安全说明、Issue / PR 模板和 GitHub Actions 检查。
- 标准库 `unittest` 测试：金额解析、日期时间、配对、DOCX/PDF 导出和 HTTP 接口。

### 变更

- README 重写为「快速开始 / 使用方法 / 局限性 / 常见问题」结构，并补齐中英文说明。
- 清理 FastAPI 时代的 ASGI 兼容层和无效的命令行参数。

### 移除

- 内部 AI 协作规则文件与旧构建产物，不再随源码仓库分发。

## [1.3.1] - 2026-10-28

### 变更

- OCR 切换为 RapidOCRLite 原生 CPU 引擎与 PP-OCRv6 tiny 模型，保留方向分类器。
- 运行包不再包含 OpenCV、Shapely 或 ONNX Runtime；模型与词典约 6.61 MiB。
- Linux 安装包面向 Ubuntu 20.04 / 22.04 / 24.04 LTS，Windows 包面向 Windows 10/11 x64。

### 修复

- 倒置文字、中文/英文混排和禁用网络后的识别回归。
- 分笔支付、未配对截图和待核对截图的导出流程。
