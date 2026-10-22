# 棕色尘埃2 日常作战台

0.2.15 · 2026-10-22 第二次热修预览 · 非官方 Windows 本地视觉辅助

[下载完整便携包与源码](https://github.com/nibushiyigeren123-bot/brown-dust2-assistant/releases/tag/v0.2.15-hotfix.20261022.2) · [更新与升级说明](docs/RELEASE_0.2.15-hotfix.20261022.2.md)

读取游戏画面、日期和本地账本，默认只读。现有 CPU OCR 小模型、页面参考匹配与宿主控制器共同完成既定流程，无独立模型服务。全流程共用 55 张完整参考图，头像、昵称和战力区域统一排除；普通截图与 OCR 留在有界内存中，关键提交和结果图像先保存再写账本。

本次修复奖励弹窗因 OCR 波动而卡住：标题、返回提示、蓝色横带共同确认后直接识别弹窗。“进入主菜单”连续两帧确认主页后立即结束并推进队列，不附带领奖或其他模块；每日任务与通行证由明确选择的模块执行。

游戏日按北京时间 08:00 刷新。保留原停止队列、历史 pending 和已完成消费；中断后“继续未运行项”只处理原队列未运行项目，不自动重放已完成或失败的消费。停止/F8、鼠标移动、窗口几何、前景和换日检查保留。Boss 和邮箱保持手动。

首次使用解压完整 Portable 目录后运行 BD2Assistant.exe。升级先关闭助手并备份原安装，再更新程序文件，保留原 config、runtime、evidence，不用默认配置覆盖。请从正常权限的安装目录运行，不能只拿 EXE。

源码建议使用 Windows x64 Python 3.12。安装 requirements.txt 后执行 `python -X utf8 tools/assistant_app.py`；构建另装 build-requirements.txt，执行 `python -X utf8 tools/build_desktop.py`。系统 OCR 需要对应识别语言，CPU ONNX Runtime 已列入依赖。

本次 133 项离线检查、冻结包自检、保存画面 worker 及一次奖励窗口只读识别通过；奖励弹窗关闭另加 SendInput 路径，实机关闭尚未验证。没有为发布点击游戏、恢复 pending 或运行整轮日常；真实执行仍待用户调试，见发布说明。源码包不含私人配置、账本、原始账号帧或本机维护文档。原创代码采用 [MIT](LICENSE)，其他素材、模型与依赖见 [NOTICE.md](NOTICE.md)。
