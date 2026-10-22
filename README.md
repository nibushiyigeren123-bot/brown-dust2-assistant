# 棕色尘埃2 日常作战台

0.2.15 · 2026-10-22 第三次热修预览 · 非官方 Windows 本地视觉辅助

[下载完整便携包与源码](https://github.com/nibushiyigeren123-bot/brown-dust2-assistant/releases/tag/v0.2.15-hotfix.20261022.3) · [更新与升级说明](docs/RELEASE_0.2.15-hotfix.20261022.3.md)

本次修复每日/每周领奖 pending 阻塞独立模块：规范记录保持原样，经营、公会、狩猎等独立任务可继续，各自的完成、消费与资源检查保留。未知、损坏、错误身份、错误路径和同日重复账本仍停步。显式领奖模块仍核对自己的 pending，不自动重放旧队列。

继续使用现有内嵌 CPU 小模型与 55 张完整参考图，排除头像、昵称和战力区域。普通帧与 OCR 留在有界内存中，关键提交和结果图像先保存再写账本。奖励弹窗按图确认，进入主菜单双帧确认后立即推进，不附带顺手领奖；奖励弹窗关闭使用 SendInput 的实际效果仍待用户反馈。

游戏日按北京时间每天 08:00 刷新。保留历史 pending、停止记录和已完成消费。停止/F8、鼠标移动、窗口几何、前景及换日检查保留，Boss 和邮箱手动。

解压完整 Portable 后运行 BD2Assistant.exe；默认只读。升级先关闭助手并备份原安装，只更新程序文件，保留 config、runtime、evidence，不用默认配置覆盖。使用正常权限安装目录，不能只拿 EXE。

源码建议 Windows x64 Python 3.12，安装 requirements.txt 后执行 `python -X utf8 tools/assistant_app.py`。构建另装 build-requirements.txt，执行 `python -X utf8 tools/build_desktop.py`。系统 OCR 需对应识别语言；CPU ONNX Runtime 已列入依赖。

本次 47 项离线检查、冻结 guard 对原账本的只读核对、打包自检及保存画面 worker 通过。没有执行新日常、领取或消费，不代表实机整轮通过。源码不含私人配置、账本或原始账号帧。原创代码采用 [MIT](LICENSE)，其他素材、模型和依赖见 [NOTICE.md](NOTICE.md)。
