"""Show recognized state and the actual reason a worker did not act."""
import json
from pathlib import Path


STATES = {
    'main_menu': '主菜单', 'unknown': '暂未识别',
    'signin_event': '每日登录奖励', 'signin_pass': '通行证登录奖励',
    'menu_overlay': '主菜单上的弹窗或遮罩', 'other_page': '当前为其他页面',
    'loading_or_unknown_overlay': '疑似加载或未知遮罩（等待，不点击）',
    'page_with_back':'带返回箭头的页面','page_with_circle_back':'活动首页返回入口',
    'talk_panel':'格鲁TALK面板','equipment_filter':'装备筛选','equipment_detail':'装备详情',
    'pass_panel': '通行证面板（已核对专属页签）',
    'mirror_settlement':'镜中战赛季结算提示','page_with_narrow_back':'带返回箭头的战斗准备页',
    'guild_page': '公会页面', 'guild_check_in_receipt': '公会签到成功提示',
    'management_panel': '经营面板', 'gacha_page': '抽抽乐页面',
    'hunt_map': '狩猎地图', 'hunt_dialog': '快速狩猎窗口',
    'mirror_preparation': '镜中之战准备页', 'mirror_ready':'镜中之战准备页',
    'mirror_lobby':'镜中之战大厅', 'mirror_auto_dialog':'镜中之战自动战斗设置',
    'mirror_multiplier_dialog':'鲜血鸡尾酒倍率设置', 'mirror_result':'镜中之战结算',
    'goddess_collection':'游戏卡珍藏集','goddess_scene':'女神像场景',
    'goddess_options':'女神像互动选项',
}
REASONS = {
    'Hunting daily task was not automatically verified':'每日狩猎任务尚未自动核对，不提交。',
    'Hunting resource balance could not be read':'当前免费或额外体力余额未读清，未提交狩猎。',
    'Not enough observed hunting resource':'当前识别到的可用体力不足，未提交狩猎。',
    'Hunting resource differs from target category':'体力图标与目标类别不一致，停止核对，未提交。',
    'Cannot determine free hunting count from observed balance and unit price':'未读清免费余额或单次消耗，无法计算次数，未提交。',
    'Mirror multiplier value could not be read':'未读清鲜血鸡尾酒倍率，未提交战斗；请关闭遮挡后重试。',
    'Mirror battle count could not be read':'未读清自动战斗次数，未提交战斗。',
    'Mirror start button resource cost could not be read':'未读清开战按钮的鸡尾酒消耗，未提交战斗。',
    'Next mirror battle would exceed the resource budget':'下一场会超过设置的鸡尾酒总额度，已停止。',
    'Not enough free mirror resource':'当前免费鸡尾酒不足，已停止，未使用额外资源。',
    'Mirror resource budget reached':'已达到设置的鸡尾酒总额度。',
    'Could not open the mirror cartridge collection':'未能打开游戏卡珍藏集，请核对主页右下箭头是否被遮挡。',
    'Mirror Wars cartridge is not visible':'未在游戏卡珍藏集中找到镜中之战卡带。',
    'Mirror red platform could not open the preparation screen':'未能通过红色平台进入镜中之战准备页。',

    "Confirm whether today's fixed hunting daily is already done":
        '请打开“日常设置→快速狩猎”，确认今天的狩猎任务是“未完成”还是“已完成”。',
    'Choose a hunting stage; the recording stage is not a user preference':
        '请在“日常设置→快速狩猎”填写要打的关卡和本轮资源额度。',
    'menu_not_verified_within_timeout': '等待超时，尚未确认返回主菜单。',
    'management_not_verified_within_timeout': '等待超时，经营流程尚未确认完成。',
    'free_draw_not_verified_within_timeout': '等待超时，免费抽取流程尚未确认完成。',
    'timeout_without_verified_completion': '等待超时，请核对当前页面是否符合所选功能的起始条件。',
    'F8': '已按F8停止。',
    'Equipment has a pending action; reconcile it without retrying':'装备存在未决操作，须先核对回执，不会重复提交。',
    'Equipment cycle is complete; return to menu without repeating it':'当前配置周期的装备任务已完成，请返回主菜单；不会重复精炼、分解或初始化。',
    'General equipment execution is not yet released':'本版已接入装备识别与进度；通用自动执行流程待接入，本次未操作游戏。',
    'All five holy-stone inventories must be read before choosing the least': '五种圣石库存没有全部读清，未自动选择；可重新检查，或改为指定元素。',
    'Hunting difficulty could not be read': '未读清左下角难度，已停止调整。',
    'Configured hunting difficulty was not reached': '未能切换到配置的难度，请核对左下角。',
    'No verified interaction glyph; do not mark it done': '未找到女神像互动图标。请确认角色已停在女神像旁，当前尚未确认签到完成。',
    'Main-menu collection entry was not recognized':'未识别到主菜单右下角的游戏卡珍藏集入口。',
    'Could not open the card collection after three attempts':'尝试3次仍未打开游戏卡珍藏集，请检查是否有弹窗或遮挡。',
    'Fantasia Square card is not visible in the collection':'游戏卡珍藏集内未找到奇幻广场卡带，尚未进入女神像场景。',
    'Fantasia Square did not load after its entry was clicked':'已选择奇幻广场，但未能确认场景加载完成。',
    'Square entry returned to menu without a verified interaction':'进入奇幻广场后又回到主页，尚未确认女神像互动。',
    'Waiting for Fantasia Square to load':'已选择奇幻广场，正在等待场景加载。',
    'Requested goddess action is disabled':'当前设置没有启用所选的女神像互动。',
    'User must stand beside the goddess statue': '请先让角色停在女神像旁，并在日常设置中确认这个准备条件。',
    'Remaining reward may require other challenges first': '当前没有所选的签到或奖励选项；可能已签到，或日常奖励还未开放，未标记完成。',
}


def readable_reason(value):
    if isinstance(value, dict):
        value = value.get('reason') or value.get('status') or str(value)
    return REASONS.get(str(value), str(value))


def summarize_report(report, task_id):
    observations = report.get('observations', [])
    last = observations[-1] if observations else {}
    state = last.get('state') or last.get('observation', {}).get('state')
    prefix = '已识别：' + STATES.get(state, state) + '。' if state else ''
    if task_id == 'equipment':
        progress=report.get('progress',{})
        stages=' / '.join(label+('已完成' if progress.get(key) else '未完成') for key,label in
                         (('refine_completed','精炼'),('decompose_completed','分解'),('filter_initialized','初始化')))
        prefix+='缓存日期 '+report.get('cache_date','未知')+'：'+stages+'。'
    if not report.get('execute'):
        if report.get('error'):
            return prefix + '只读检查异常：' + str(report['error'])
        if report.get('stopped'):
            return prefix + '只读检查已停步：' + readable_reason(report['stopped']) + ' 没有点击游戏。'
        if task_id == 'navigation' and state and state != 'main_menu':
            return prefix + '只读模式仅识别，不会关闭弹窗或进入主菜单。若要进入主菜单，请切换实机操作，再点“执行选中单段”。'
        return prefix + '只读检查结束，没有点击游戏。'
    if report.get('completed') and task_id == 'task_rewards':
        return prefix + '每日及每周可领奖励已检查并返回主页；未完成任务保留。下次运行会重新检查每周。'
    if report.get('completed'):
        if report.get('unavailable')=='season_settlement':
            from mirror_status import settlement_from_report
            if not settlement_from_report(report, report.get('cache_date')):
                return prefix+'赛季末回执尚未完整核实，请查看任务进度与运行记录。'
            return prefix+'赛季末 · 已跳过。'+(report.get('skip_reason') or '已确认结算提示并返回主菜单，继续其他日常；未记作战斗完成。')
        if task_id in ('goddess_check_in','goddess_daily_reward') and report.get('deferred_actions'):
            done=report.get('progress',{}).get('goddess',{}).get('done',[])
            if 'check_in' in done and 'daily_reward' in report['deferred_actions']:
                return prefix+'签到已完成，当前没有可领的日常奖励，已返回主菜单；完成其他关卡后可再检查。'
            return prefix+'当前未发现可领取的女神像互动，已返回主菜单；尚未核实的项目保留待检查。'
        if task_id == 'guild' and not report.get('actions'):
            return prefix + '本游戏日已有签到成功记录，本次未重复进入公会。'
        if report.get('actions'):
            return prefix + '任务完成。'
        return prefix + '已满足完成条件，本次无需点击。'
    stopped = report.get('stopped') or report.get('error')
    if isinstance(stopped, dict):
        reason = stopped.get('reason') or stopped.get('status', '')
        category = {'needs_configuration': '需要设置：', 'needs_review': '需要核对：',
                    'waiting_for_option':'等待互动：','skipped': '已跳过：'}.get(stopped.get('status'), '任务停止：')
    else:
        reason, category = stopped or '未确认完成，请打开运行记录查看详情。', '任务停止：'
    return prefix + category + REASONS.get(reason, reason)


def worker_feedback(output, task_id):
    output = Path(output)
    path = output / 'run.json'
    if path.is_file():
        report = json.loads(path.read_text(encoding='utf-8'))
        return summarize_report(report, task_id)
    log = output / 'worker.log'
    if log.is_file():
        lines = [line.strip() for line in log.read_text(encoding='utf-8').splitlines() if line.strip()]
        if lines:
            return '启动失败：' + lines[-1]
    return '任务未产生识别记录，请打开运行记录查看原因。'
