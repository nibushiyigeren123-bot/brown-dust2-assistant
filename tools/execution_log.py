"""Read-only, incremental presentation of worker receipts. Never issues game input."""
import codecs
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from module_catalog import BY_ID
from task_feedback import STATES, REASONS

LABELS = {'queued': '待运行', 'running': '正在核对', 'verified': '已核实',
          'inspected': '只读检查结束', 'needs_review': '需要核对', 'paused': '已暂停',
          'stopped': '已停止', 'complete': '队列完成', 'completed': '已完成',
          'needs_configuration': '需要设置', 'skipped': '已跳过',
          'blocked': '受阻，未执行', 'complete_with_errors': '执行结束，有错误'}
PAGES = dict(STATES, daily_tasks='每日任务', weekly_tasks='每周任务',
             pass_tasks='通行证任务', pass_levels='通行证等级奖励', reward_overlay='获得奖励')


def readable(value):
    if isinstance(value, dict):
        return ' · '.join(readable(value[k]) for k in ('status', 'reason', 'message') if value.get(k)) or json.dumps(value, ensure_ascii=False)
    return REASONS.get(str(value), LABELS.get(str(value), str(value)))


@dataclass
class LogEvent:
    module: str
    level: str
    message: str
    time: str = field(default_factory=lambda: datetime.now().strftime('%H:%M:%S'))

    def line(self):
        level = {'info': '过程', 'wait': '等待', 'warning': '核对', 'error': '错误', 'success': '结果'}[self.level]
        return f'{self.time}  [{self.module}]  {level}  {self.message}\n'


class ReceiptLog:
    def __init__(self, output, task_id):
        self.output, self.task_id = Path(output), task_id
        self.cache, self.previous, self.offsets, self.wait_times = {}, {}, {}, {}
        self.texts = {}
        self.events = []
        self.paths = {}

    def emit(self, module, level, message):
        self.events.append(LogEvent(BY_ID[module].name if module in BY_ID else module, level, str(message)))

    def changed(self, key, value):
        if self.previous.get(key) == value:
            return False
        self.previous[key] = value
        return True

    def read_json(self, path):
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if self.cache.get(path, (None,))[0] == signature:
                return self.cache[path][1]
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict):
                return None
            self.cache[path] = (signature, data)
            return data
        except (OSError, ValueError):
            # Atomic replacement / a half-written receipt is not a task failure.
            return None

    def scalar(self, path, module, key, value, level, label):
        if self.changed((path, key), value) and value:
            self.emit(module, level, label + readable(value))

    def report(self, path, module):
        report = self.read_json(path)
        if report is None:
            return
        for field_name in ('observations', 'actions'):
            if field_name not in report:
                continue
            records = report.get(field_name, [])
            if not isinstance(records, list):
                continue
            key = (path, field_name)
            offset = self.offsets.get(key, 0)
            checkpoint = json.dumps(records[offset-1], sort_keys=True, ensure_ascii=False) if offset and len(records) >= offset else None
            if len(records) < offset or (offset and checkpoint != self.previous.get((key, 'checkpoint'))):
                offset = 0
                self.emit(module, 'warning', '回执列表已更新，从当前记录继续显示。')
            for record in records[offset:]:
                if not isinstance(record, dict):
                    continue
                if field_name == 'observations':
                    self.observation(path, module, record)
                else:
                    self.emit(module, 'info', '操作回执：' + json.dumps(record, ensure_ascii=False))
            self.offsets[key] = len(records)
            if records:
                self.previous[(key, 'checkpoint')] = json.dumps(records[-1], sort_keys=True, ensure_ascii=False)
        if isinstance(report.get('observation'), dict):
            self.observation(path, module, report['observation'])
        waiting = report.get('waiting')
        if waiting:
            message = waiting.get('message', readable(waiting)) if isinstance(waiting, dict) else str(waiting)
            now = time.monotonic()
            if now - self.wait_times.get(path, -100) >= 10:
                self.emit(module, 'wait', message)
                self.wait_times[path] = now
        else:
            self.wait_times.pop(path, None)
        for key, level, label in (('error', 'error', '执行异常：'), ('stopped', 'warning', '停止原因：'),
                                  ('pending_input', 'warning', '等待核对：'), ('pending', 'warning', '未决操作：')):
            self.scalar(path, module, key, report.get(key), level, label)
        self.scalar(path, module, 'completed', '已完成' if report.get('completed') else None, 'success', '回执确认：')

    def observation(self, path, module, record):
        observation = record.get('observation', record)
        if not isinstance(observation, dict):
            return
        parts = []
        if observation.get('state'):
            parts.append('识别：' + PAGES.get(observation['state'], observation['state']))
        if observation.get('resource') in ('rice','torch') and observation.get('free_available') is not None:
            name={'rice':'米饭','torch':'火把'}[observation['resource']]
            extra=observation.get('extra_display')
            parts.append(name+'：免费 '+str(observation['free_available'])+'，额外 '+(str(extra) if extra is not None else '待识别'))
        if observation.get('stage_text_raw'):parts.append('当前关卡：'+observation['stage_text_raw'])
        for key in ('claimable', 'count', 'cost', 'free_only', 'pass_id'):
            if key in observation:
                label = {'claimable': '可领取', 'count': '次数', 'cost': '消耗', 'free_only': '仅免费', 'pass_id': '通行证'}[key]
                parts.append(label + '=' + str(observation[key]))
        inventory=observation.get('stone_inventory')
        if isinstance(inventory,dict) and len(inventory)==5:
            parts.append('圣石库存：'+' / '.join(name+': '+str(value) for name,value in inventory.items()))
            parts.append('当前最少：'+min(inventory,key=inventory.get)+'（执行器另做双帧核对）')
        decision = record.get('decision') or observation.get('decision')
        if decision:
            parts.append('判断：' + readable(decision))
        message = '；'.join(parts)
        if message and self.changed((path, 'observation'), message):
            self.emit(module, 'info', message)

    def tail(self, path, module, final=False):
        if not path.is_file():
            return
        state = self.texts.setdefault(path, {'offset': 0, 'decoder': codecs.getincrementaldecoder('utf-8')(), 'pending': ''})
        try:
            with path.open('rb') as stream:
                if path.stat().st_size < state['offset']:
                    state.update(offset=0, decoder=codecs.getincrementaldecoder('utf-8')(), pending='')
                    self.emit(module, 'warning', path.name + ' 已重新写入。')
                stream.seek(state['offset'])
                data = stream.read(256 * 1024)
                state['offset'] += len(data)
            value = state['pending'] + state['decoder'].decode(data)
        except (OSError, UnicodeError) as error:
            self.scalar(path, module, 'read_error', str(error), 'error', '日志读取失败：')
            return
        lines = value.split('\n')
        state['pending'] = lines.pop()
        # Flush only when all bytes have been drained, even for large final logs.
        if final and state['offset'] == path.stat().st_size and state['pending']:
            lines.append(state['pending']); state['pending'] = ''
        for line in lines:
            if line.strip():
                try:
                    payload = json.loads(line)
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and not payload.get('error') and not payload.get('stopped'):
                    # Structured reports are already displayed above; avoid dumping
                    # geometry, paths and entire ledgers into the readable viewer.
                    if any(key in payload for key in ('state', 'observations', 'observation', 'completed', 'status')):
                        continue
                level = 'error' if any(word in line.lower() for word in ('traceback', 'error', 'exception', '错误', '失败')) else 'info'
                self.emit(module, level, line.rstrip('\r'))

    def collect(self, final=False):
        self.events = []
        workflow = self.read_json(self.output / 'workflow.json')
        if workflow:
            for item in workflow.get('tasks', []):
                module = item.get('id', '队列')
                state = item.get('status')
                key = ('task', module, item.get('output'))
                if state != 'queued' and self.changed(key, state):
                    self.emit(module, 'warning' if state in ('needs_review', 'blocked') else 'success' if state in ('verified', 'inspected') else 'info', LABELS.get(state, str(state)))
                if item.get('output'):
                    self.paths[Path(item['output'])] = module
            self.scalar(self.output, '队列', 'status', workflow.get('status'), 'info', '队列状态：')
            self.scalar(self.output, '队列', 'reason', workflow.get('reason'), 'warning', '队列说明：')
            if workflow.get('message') and time.monotonic() - self.wait_times.get('workflow', -100) >= 10:
                self.emit('队列', 'wait', workflow['message']); self.wait_times['workflow'] = time.monotonic()
        self.paths[self.output] = self.task_id if self.task_id != 'workflow' else '队列'
        for directory, module in self.paths.items():
            self.report(directory / 'run.json', module)
            self.report(directory / 'inspection.json', module)
            self.report(directory / 'worker_failure.json', module)
            for name in ('worker.log', 'process.log'):
                self.tail(directory / name, module, final)
            result = self.read_json(directory / 'worker_result.json')
            if result and result.get('exit_code'):
                self.scalar(directory, module, 'exit', result['exit_code'], 'error', '执行器退出码：')
        if workflow and workflow.get('summary') and workflow.get('status') != 'running':
            summary = workflow['summary']
            token = json.dumps(summary, sort_keys=True, ensure_ascii=False)
            if self.changed(('workflow', 'final_summary'), token):
                title = '执行结束，有错误' if summary['failed'] or summary['blocked'] else '执行结果汇总'
                if not summary['traversed']:
                    title = '队列已中断 · 当前结果汇总'
                self.emit('队列', 'warning' if summary['failed'] or summary['blocked'] or not summary['traversed'] else 'success',
                    f"{title}：成功 {summary['success']}，失败 {summary['failed']}，受阻 {summary['blocked']}，跳过 {summary['skipped']}，未运行 {summary['remaining']}。")
                if summary.get('control_reason'):
                    self.emit('队列', 'warning', '整轮中断原因：' + readable(summary['control_reason']))
                for error in summary['errors']:
                    self.emit(error['module'], 'error', readable(error['reason']))
                    self.emit(error['module'], 'warning', ('尚有未决：' + '；'.join(
                        f"{entry['module']} / {entry.get('op_id') or '无操作ID'} / {entry['ledger']}" for entry in error['pending']))
                        if error['pending'] else '本项回执未列出未决；请结合运行记录核对。')
                    self.emit(error['module'], 'info', '运行记录：' + str(error.get('output') or self.output))
                    self.emit(error['module'], 'warning', error['action'])
        return self.events
