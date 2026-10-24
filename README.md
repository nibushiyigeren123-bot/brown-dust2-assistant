# 棕色尘埃2 日常作战台

**1.0.0 正式版** · Windows 本地视觉辅助 · 非官方项目

[下载完整便携程序与源码](https://github.com/nibushiyigeren123-bot/brown-dust2-assistant/releases/tag/v1.0.0) · [发布及升级说明](docs/RELEASE_1.0.0.md)

读取游戏画面、日期和本地进度账本；首次使用先核对模块设置。启动界面不会自动执行，点击后才启动任务进程。只读模式不发送输入；实际操作与资源使用受模块配置及运行保护约束。未知页面停步留证，Boss和邮箱保留手动。

内嵌模型使用 CPU，在任务进程中按需加载，无独立模型服务。参考图对头像、昵称和战力区域统一遮黑。游戏日按北京时间每日08:00刷新，保留消费、完成及未决账本；继续队列不重放已完成项目。

本版保留 r8d 行为，修复女神广场五秒流程、主页活动解锁入口和通行证直接领取流程。通行证仅记录动作完成，不核对奖励到账。定向实机与公开包只读检查范围详见发布说明；本次未重新跑完整十一项日常队列。

首次使用请完整解压 Portable 后运行 BD2Assistant.exe。升级前关闭助手并备份旧目录，保留原 config、runtime、evidence，不用默认配置覆盖个人设置。

源码建议使用 Windows x64 Python 3.12。安装 requirements.txt 后运行 `python -X utf8 tools/assistant_app.py`；首次启动从 templates/config 创建通用配置。安装 build-requirements.txt 后运行 `python -X utf8 tools/build_desktop.py` 构建便携程序。Windows系统OCR需安装对应识别语言。

`--self-check <目录>` 验证模板、配置、规则和十一项模块注册；`--step-vision-self-check <目录>` 验证公开CPU参考向量库。这些命令不会进行游戏实机操作。

公开源码不包含账号资料、个人配置、运行账本、日志或私人测试证据；包含脱敏参考图、通用模板和分发所需模型。原创代码采用 [MIT](LICENSE)，游戏素材、模型及依赖权利见 [NOTICE.md](NOTICE.md)。
