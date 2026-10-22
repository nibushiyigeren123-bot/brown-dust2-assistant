# 棕色尘埃2 日常作战台

0.2.15 · 2026-10-22 热修预览版 · 非官方 Windows 本地视觉辅助 · 持续更新

[下载完整便携包与源码](https://github.com/nibushiyigeren123-bot/brown-dust2-assistant/releases/tag/v0.2.15-hotfix.20261022) · [更新与升级说明](docs/RELEASE_0.2.15-hotfix.20261022.md)

读取游戏画面、日期和本地进度账本，默认只读。实际输入与资源消费按模块设置启用。停止／F8、鼠标移动、窗口和换日检查保留；未知页面停步。Boss 和邮箱保持手动。

这版加入 55 张共用页面参考图，头像、昵称和战力区域统一置黑；CPU 模型与 OCR 在任务进程内工作，不另开模型服务。普通帧与 OCR 主要留在有界内存中，关键提交和结果图像先保存再写账本。队列按当前项目的主菜单目标识别和返回，确认到达后推进。它使用参考匹配和已核实控件，能力边界见发布说明。

游戏日按北京时间每天 08:00 刷新。新一天不继承旧日完成标记；保留历史消费、完成和分阶段账本以及 pending。清理只针对可丢弃旧缓存。重复只读检查复用自己的检查目录，不覆盖真实运行队列。中断后“继续未运行项”只处理原队列未运行的项目；不重放失败或已完成消费，不清未决记录。

首次使用请解压 Portable 全目录后运行 BD2Assistant.exe。升级前关闭助手并备份原安装，再更新程序文件，保留原 config、runtime、evidence；不能只复制 EXE，也不要用默认配置覆盖自己的设置。

源码运行建议使用 Windows x64 Python 3.12（当前便携构建的 Python 版本）。安装 requirements.txt 后执行 `python -X utf8 tools/assistant_app.py`。构建需再安装 build-requirements.txt，执行 `python -X utf8 tools/build_desktop.py`。Windows 系统 OCR 需要对应识别语言；CPU ONNX Runtime 依赖已列入 requirements.txt。首次启动从 templates/config 生成通用配置。

`--self-check <目录>` 验证模板、配置和规则。8 组 385 项离线检查、最终冻结包和保存脱敏画面的只读 worker 已通过；完整新版实机日常、实时导航与优化耗时仍待用户测试。日常与消费不能为发布而重复执行。

源码包包含脱敏的完整参考帧与局部模板，不包含原始账号截图、私人配置、运行账本、历史维护交接或本机路径。原创代码采用 [MIT](LICENSE)，游戏素材、模型及依赖见 [NOTICE.md](NOTICE.md)。
