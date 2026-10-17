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
           ('识别当前画面', '处理已识别的返回入口', '确认主菜单'), '建立后续任务的共同起点。'),
    Module('management', '经营收益', 'm01_management', (),
           ('进入经营面板', '检查当前可领状态', '领取并核对奖励', '返回主菜单'), '有收益才领取，灰色按钮跳过。'),
    Module('free_draw', '服装与装备免费抽取', 'm02_free_draw', (),
           ('检查服装免费入口', '检查装备免费入口', '确认免费结果', '返回主菜单'), '分别检查两类免费入口，保持已有完成记录。', True),
    Module('guild', '公会签到', 'guild_check_in', (),
           ('核对本周期签到记录', '进入公会', '识别签到回执', '返回主菜单'), '本周期只进入一次；邮箱手动。'),
    Module('hunting', '狩猎场与额外狩猎', 'pending_daily', ('--module', 'hunting'),
           ('读取关卡与资源配置', '核对当前完成进度', '设置难度和资源', '执行并核对结果', '返回主菜单'), '保留当前日常目标与额外计划，受资源预算约束。', True),
    Module('goddess_check_in', '女神像签到', 'pending_daily', ('--module', 'goddess', '--checkpoint', 'check_in'),
           ('进入奇幻广场', '识别女神像互动', '签到与回执核对', '返回主菜单'), '已有签到记录不重复；检查当前可领项目。'),
    Module('mirror', '镜中之战', 'pending_daily', ('--module', 'mirror'),
           ('核对本周期完成记录', '进入镜中大厅', '设置次数、倍率和资源', '核对结算', '返回主菜单'), '使用原阵容与配置额度，正常战斗流程。', True),
    Module('equipment', '装备精炼与分解', 'equipment_tasks', (),
           ('读取本周期装备进度', '核对单件精炼', '单选R优先，N后备', '单件分解与筛选恢复', '返回主菜单'), '先查账本；已完成的精炼、分解与初始化不重做。', True),
    Module('event', '活动战斗', 'event_tasks', (),
           ('核对活动与共享AP', '检查普通或挑战入口', '选择快速或正常战斗', '核对次数、资源和结果', '返回主菜单'), '普通与挑战共享额度；仅使用配置允许的持有资源。', True),
    Module('goddess_daily_reward', '女神像日常奖励', 'pending_daily', ('--module', 'goddess', '--checkpoint', 'daily_reward'),
           ('返回女神像', '检查当前可领奖励', '领取并核对', '返回主菜单'), '在其他挑战之后检查，未开放的项目明确保留。', refresh_each_run=True),
    Module('rewards', '通行证奖励', 'pass_rewards', (),
           ('检查特别赛季任务', '检查特别赛季等级', '检查活动通行证任务', '检查活动通行证等级', '返回主菜单'), '任务经验可能升级，每本都复查等级奖励；邮箱手动。', refresh_each_run=True),
    Module('task_rewards', '每日与每周任务奖励', 'task_rewards', (),
           ('检查每日任务与区间奖励', '检查每周已完成任务', '检查每周区间奖励', '复查列表并返回主菜单'), '每次每日领奖都顺查每周，未完成任务保留。', refresh_each_run=True),
    Module('boss', 'Boss战', None, (), (), '入口预留，后续适配。', reserved_reason='当前留作扩展，不加入日常循环。'),
    Module('mirror_settlement', '镜中赛季结算', None, (), (), '赛季末状态入口预留。', reserved_reason='等待赛季结算真实画面后适配。'),
    Module('character_pass', '角色通行证', None, (), (), '独立按天计算的通行证入口。', reserved_reason='按天规则与实际入口待后续样本。'),
)
BY_ID = {module.id: module for module in MODULES}


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
