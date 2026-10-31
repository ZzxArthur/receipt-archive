## 改了什么

<!-- 一句话说明改动和原因。 -->

## 关联问题

<!-- 例如：Closes #12 -->

## 验证方式

- [ ] `python -m unittest discover -s tests -v` 通过
- [ ] `python -m compileall -q receipt_tool desktop windows scripts` 通过
- [ ] 手动验证了受影响的界面 / 导出流程（说明平台）

## 检查清单

- [ ] 没有加入云端 OCR、遥测或默认联网行为
- [ ] 没有提交真实截图、个人信息或识别结果
- [ ] 金额仍然使用整数分，匹配仍然只接受唯一解
- [ ] 新增依赖 / 模型 / 字体已说明来源和许可证
