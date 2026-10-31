凭证归档（轻量化便携版，版本见 VERSION）

Windows 便携版

适用 Windows 10/11 x64。请将整个 ZIP 解压到可写目录，不要在压缩包中直接运行。

双击 run.vbs 打开独立桌面窗口；run.cmd 可显示启动错误。
双击 run-browser.cmd 可使用默认浏览器操作同一个本机工作区。
Python、OCR 模型及精简运行依赖已经包含，无需另外安装 Python、OpenCV 或 Office。

独立桌面窗口自动调用系统内置 Microsoft Edge 应用窗口模式（WebView2 内核）。
原生 OCR 引擎 需要 Microsoft Visual C++ v14 x64 Runtime。
若电脑缺少该组件，请从 Microsoft 官方页面安装后再打开：
https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist

截图可以逐张或分次添加，全部添加后点击“开始识别”。
先核对金额和配对，再导出 Word + PDF；系统会将订单放在上方、支付截图放在下方。
同图自动去重；同一订单支持多张支付截图。重新打开可继续上次批次。

数据默认保存在 %LOCALAPPDATA%\ReceiptArchive\data。
窗口配置保存在同级 webview，日志是 backend.log 和 desktop.log。
关闭窗口后后台可以继续完成识别；关闭窗口不会删除截图或识别结果。
“清空本批次”只删除应用保存的当前批次，源图片和已导出的文档自行保留。

第三方许可证保留在 runtime/LICENSE.txt 和各依赖的 *.dist-info 目录。

OCR 使用 RapidOCRLite 5.0.0a15（上游预发布版）及 PP-OCRv6 tiny 离线模型。
本包已在 Linux 上完成 Windows 交叉构建和文件校验，尚未在 Windows 实机验证。
