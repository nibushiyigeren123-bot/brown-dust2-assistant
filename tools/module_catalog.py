"""Declarative module contracts shared by the UI and serial coordinator."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Module:
    id: str
    name: str
    worker: str | None
    arguments: tuple
    steps: tuple
    description: str
    consumes: bool = False
    refresh_each_run: bool = False
    reserved_reason: str = ''


MODULES = (
    Module('navigation', '进入主菜单', 'common_navigation', (),
           ('识别当前页面与已知弹窗', '执行已确认的返回入口', '连续两帧确认主菜单', '到达主菜单即结束导航并推进队列'), '只负责回到主菜单；进度核对与领奖由所选模块负责。'),
    Module('management', '经营收益', 'm01_management', (),
           ('进入经营面板', '检查当前可领状态', '领取并核对奖励', '返回主菜单'), '有收益才领取，灰色按钮跳过。'),
    Module('free_draw', '服装与装备免费抽取', 'm02_free_draw', (),
           ('检查服装免费入口', '检查装备免费入口', '确认免费结果', '返回主菜单'), '分别检查两类免费入口，保持已有完成记录。', True),
    # [功能] 说明对应公会本人恢复流程：先读未决与当前页，已完成不重复；不改变执行规则。
    Module('guild', '公会签到', 'guild_check_in', (),
           ('核对未决与当前页', '必要时恢复未完成入场', '识别收据或确认无收据访问', '返回主菜单'),
           '已完成不重复；必要时恢复未完成入场；邮箱手动。'),
    Module('hunting', '狩猎场与额外狩猎', 'pending_daily', ('--module', 'hunting'),
           ('核对每日狩猎进度与已有回执', '读取米饭与火炬余额', '固定1次与指定史莱姆／哥布林／圣石计划', '提交前重读资源并核对结果', '返回主菜单'), '先核对再连续执行；固定1次仅免费，额外刷取独立配置。', True),
    # [功能] 同次广场访问处理已启用的签到与奖励，真实完成账本防止重复点击。
    Module('goddess_check_in', '女神像签到与奖励', 'pending_daily', ('--module', 'goddess', '--checkpoint', 'available'),
           ('进入奇幻广场', '识别右下F叹号或拍手', '核对签到与奖励回执', '返回主菜单'), '同次处理当前可用签到与奖励；已有真实回执不重复。'),
    Module('mirror', '镜中之战', 'pending_daily', ('--module', 'mirror'),
           ('核对本游戏日完成记录', '进入镜中场景并点击自动战斗', '识别赛季末提示或设置次数与倍率', '核对战斗结果；赛季末跳过', '返回主菜单'), '保持原阵容与配置额度；赛季末判定已合并，结算时跳过并继续日常。', True),
    Module('equipment', '装备精炼与分解', 'equipment_tasks', (),
           ('读取本周期装备进度', '核对单件精炼', '单选R优先，N后备', '单件分解与筛选恢复', '返回主菜单'), '先查账本；已完成的精炼、分解与初始化不重做。', True),
    Module('event', '活动战斗', 'event_tasks', (),
           ('核对活动与共享AP', '检查普通或挑战入口', '选择连续或快速战斗', '核对次数、资源和结果', '返回主菜单'), '普通与挑战共享额度；仅使用配置允许的持有资源。', True),
    Module('rewards', '通行证奖励', 'pass_rewards', (),
           ('逐本点记事板与全部获得', '按设置领取等级奖励一次', '按设置继续角色通行证', '返回主菜单'), '逐本执行一次领取操作，奖励未核实；开启当期角色后同页继续；邮箱手动。', refresh_each_run=True),
    Module('task_rewards', '每日与每周任务奖励', 'task_rewards', (),
           ('检查每日任务与区间奖励', '检查每周已完成任务', '检查每周区间奖励', '复查列表并返回主菜单'), '每次每日领奖都顺查每周，未完成任务保留。', refresh_each_run=True),

)
# [兼容] 旧worker和历史报告仍能按原ID查询，但新UI与默认计划不显示重复日奖行。
LEGACY_MODULES = (
    # [角色通行证] 固定后缀与PASS结构识别；每日独立缓存，角色前缀不参与必须匹配。
    Module('character_pass','角色通行证','pass_rewards',('--scope','character'),
           ('进入角色通行证','点记事板与全部获得','按设置领取等级奖励一次','返回主菜单'),
           '执行一次领取操作，奖励未核实；角色名称可变，已有未决不补点。',refresh_each_run=True),
    Module('goddess_daily_reward', '女神像日常奖励', 'pending_daily', ('--module', 'goddess', '--checkpoint', 'daily_reward'),
           ('返回女神像', '检查当前可领奖励', '领取并核对', '返回主菜单'), '在其他挑战之后检查，未开放的项目明确保留。', refresh_each_run=True),
)
BY_ID = {module.id: module for module in (*MODULES, *LEGACY_MODULES)}

def normalize_plan(plan):
    """[内存迁移] 旧独立角色行合并为通行证奖励，保序去重；历史文件不改写。"""
    result = dict(plan)
    aliases = {'goddess_daily_reward': 'goddess_check_in', 'character_pass': 'rewards'}
    for field in ('tasks', 'selected'):
        result[field] = list(dict.fromkeys(aliases.get(key, key) for key in plan.get(field, [])))
    return result



def default_plan():
    return {'schema_version': 1, 'tasks': [m.id for m in MODULES if m.worker],
            'selected': [m.id for m in MODULES if m.worker]}


def validate_plan(plan):
    if plan.get('schema_version') != 1:
        raise ValueError('不支持的队列版本')
    order, selected = plan['tasks'], plan['selected']
    if not isinstance(order, list) or not isinstance(selected, list) or len(order) != len(set(order)) or len(selected) != len(set(selected)):
        raise ValueError('队列必须是无重复的模块列表')
    if any(key not in BY_ID or not BY_ID[key].worker for key in order + selected):
        raise ValueError('队列包含未知或预留模块')
    if not set(selected) <= set(order):
        raise ValueError('勾选项目必须存在于队列')
    if not selected:
        raise ValueError('请至少勾选一个任务')
    if 'rewards' in selected and 'task_rewards' in selected and order.index('rewards') > order.index('task_rewards'):
        raise ValueError('通行证领奖应在每日及每周领奖之前')
    return [key for key in order if key in selected]


def task_rows():
    rows = [(m.id, m.name, '已预留' if not m.worker else '可运行', m.description,
             m.worker, list(m.arguments)) for m in MODULES]
    rows.append(('workflow', '日常循环', '连续执行', '按勾选队列逐段核对。', 'workflow_runner', []))
    return rows
