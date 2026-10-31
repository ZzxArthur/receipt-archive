# 贡献指南

感谢你愿意改进「凭证归档」。这个项目优先解决一个很小的问题：把**订单截图**和**支付截图**整理成可交付的报销材料。请尽量让改动围绕这个目标。

## 开始开发

```bash
git clone https://github.com/ZzxArthur/receipt-archive.git
cd receipt-archive

uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --no-deps -r requirements-linux.lock
.venv/bin/python scripts/prepare_ocr_models.py
.venv/bin/python -m receipt_tool.app --port 8765
```

浏览器打开 <http://127.0.0.1:8765>。桌面窗口需要系统安装 GTK 3 / WebKitGTK，开发后端时不必启动它。

## 运行检查

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q receipt_tool desktop windows scripts
```

测试不依赖网络，也不读取真实用户截图。涉及 OCR 的测试使用构造的文字框，不提交截图样本。

## 设计约束

这些约束不是风格偏好，而是这个项目能离线、轻量运行的前提：

1. **默认离线**。不得加入云端 OCR、云端存储、遥测或需要 API Key 的功能。需要联网的能力（如发票查验）只能作为可选插件，并明确提示用户。
2. **标准库优先**。HTTP 服务、multipart 解析、DOCX 和 PDF 生成目前都用标准库加 Pillow 实现。新增依赖前请说明它替代了多少代码、增加多少安装体积。
3. **金额只用整数分**。不要把金额改成浮点数，也不要在匹配逻辑里做四舍五入。
4. **识别可以不确定，但不能猜**。宁可把结果标成「待核对」，也不要自动接受一个可能错误的金额或配对。
5. **用户数据不出本机**。日志、错误信息和测试都不得包含真实截图、姓名、手机号或订单号。

## 提交改动

- 一个 Pull Request 只做一件事。重构和功能分开。
- 新增或修改识别规则时，请附上脱敏后的文字布局（或构造的测试用例），说明旧行为为什么错。
- 界面改动请同时检查 1280px 宽度和窄窗口下的布局。
- 提交信息用一句话说明「改了什么、为什么」，例如：`识别「已退款」截图时不生成支出记录`。

## 报告问题

请使用 Issue 模板，并提供：

- 操作系统、安装方式（源码 / deb / Windows ZIP）和版本号；
- 复现步骤；
- 相关日志（默认不包含截图内容）；
- 如果是识别问题，请描述截图类型和期望字段，**不要上传真实发票或包含个人信息的原图**。

安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

## 许可证

提交贡献即表示你同意以 [MIT License](LICENSE) 授权你的改动。新增第三方代码、模型或字体时，必须同时补充来源和许可证，并确认允许再分发。
