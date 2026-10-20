"""Offline queue/receipt tests. Workers and game interaction are replaced."""
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'tools'))
import workflow_runner as w
from daily_tasks import guard_foreign_pending, PendingConflictError
from execution_control import ExecutionControlError, stop_execution
from execution_log import ReceiptLog
from module_catalog import default_plan
from game_calendar import game_day


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'runtime').mkdir()
        self.identity = {'cache_date': game_day().isoformat()}
        self.hashes = {'daily_tasks.json': 'original'}
        self.calls, self.failures, self.controls = [], {}, {}
        self.missing, self.corrupt = set(), set()
        self.manifest = self.root / 'plan.json'
        self.keys = ['navigation', 'management', 'free_draw', 'guild']
        w.save(self.manifest, dict(schema_version=1, tasks=self.keys, selected=self.keys))
        owner = self

        class Worker:
            pid = 12345
            def __init__(self, command, **kwargs):
                key = command[command.index('--worker') + 1]
                output = Path(command[command.index('--output') + 1])
                owner.calls.append(key)
                self.code = 1 if key in owner.failures else 0
                child = {'execute': '--execute' in command, 'completed': True,
                         'observations': [{'state': 'main_menu'}]}
                if key == 'navigation':
                    child['startup_task_audit'] = dict(owner.identity, completed=True, audit_id='test-audit')
                if key in owner.failures:
                    child.update(completed=False, error=owner.failures[key])
                if key not in owner.missing:
                    w.save(output / 'run.json', child)
                if key in owner.corrupt:
                    (output / 'run.json').write_text('{', encoding='utf-8')
                if key in owner.controls:
                    w.save(output / 'control.json', owner.controls[key])
            def wait(self):
                return self.code

        for patcher in (patch.object(w, 'ROOT', self.root),
                        patch.object(w, 'config_identity', side_effect=lambda _: (self.identity, self.hashes.copy())),
                        patch.object(w.subprocess, 'Popen', Worker),
                        patch('runtime_policy.wait_observed', return_value={}),
                        patch('game_window.check_requested_stop', return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_queue(self, name='run', execute=True, resume=False):
        return w.run(SimpleNamespace(output=self.root / 'evidence/workbench_runs' / name,
                    manifest=self.manifest, execute=execute, resume=resume))

    def test_middle_failure_continues_and_never_certifies_success(self):
        self.failures['management'] = '失败A'
        state = self.run_queue()
        self.assertEqual(self.calls, self.keys)
        self.assertEqual(state['status'], 'complete_with_errors')
        self.assertFalse(state['completed'])
        self.assertTrue(state['traversed'])
        self.assertEqual(state['summary']['failed'], 1)
        self.assertEqual(state['tasks'][1]['status'], 'needs_review')

    def test_multiple_errors_missing_and_corrupt_reports_continue(self):
        self.failures['management'] = '错误A'
        self.missing.add('free_draw')
        self.corrupt.add('guild')
        state = self.run_queue()
        self.assertEqual(self.calls, self.keys)
        self.assertEqual(state['summary']['failed'], 3)
        self.assertEqual(len(state['summary']['errors']), 3)

    def test_subprocess_launch_error_continues(self):
        original = w.subprocess.Popen
        def launch(command, **kwargs):
            if 'management' in command:
                raise OSError('不能启动当前模块')
            return original(command, **kwargs)
        with patch.object(w.subprocess, 'Popen', side_effect=launch):
            state = self.run_queue()
        self.assertEqual(self.calls, ['navigation', 'free_draw', 'guild'])
        self.assertEqual(state['summary']['failed'], 1)

    def test_sticky_f8_stops_even_after_key_release_and_exception_caught(self):
        self.failures['management'] = '用户按F8'
        self.controls['management'] = dict(kind='stopped', reason='用户按F8停止')
        state = self.run_queue()
        self.assertEqual(self.calls, ['navigation', 'management'])
        self.assertEqual(state['status'], 'stopped')
        self.assertFalse(state['traversed'])
        self.assertEqual(state['summary']['remaining'], 2)
        self.controls.clear()
        self.calls.clear()
        state = self.run_queue(resume=True)
        self.assertEqual(self.calls, ['free_draw', 'guild'])
        self.assertEqual(state['summary']['failed'], 1)

    def test_day_lock_config_control_remain_global(self):
        for kind in ('day_changed', 'lock_conflict', 'configuration', 'ledger_invalid'):
            with self.subTest(kind=kind):
                self.calls.clear()
                self.controls = {'management': dict(kind=kind, reason=kind)}
                state = self.run_queue(name=kind)
                self.assertEqual(self.calls, ['navigation', 'management'])
                self.assertFalse(state['traversed'])
                self.assertEqual(state['stop_kind'], kind)

    def test_actual_config_change_after_worker_is_global(self):
        original = w.subprocess.Popen
        def launch(command, **kwargs):
            child = original(command, **kwargs)
            if 'management' in command:
                self.hashes['daily_tasks.json'] = 'changed'
            return child
        with patch.object(w.subprocess, 'Popen', side_effect=launch):
            state = self.run_queue()
        self.assertEqual(self.calls, ['navigation', 'management'])
        self.assertFalse(state['traversed'])

    def test_pause_and_stop_start_no_worker(self):
        for name, signal in (('pause', 'pause.requested'), ('stopped', 'stop.requested')):
            output = self.root / 'evidence/workbench_runs' / name
            output.mkdir(parents=True)
            (output / signal).write_text('requested', encoding='utf-8')
            state = self.run_queue(name=name)
            self.assertEqual(self.calls, [])
            self.assertEqual(state['summary']['remaining'], 4)

    def test_changed_hash_refuses_resume_new_continuation_keeps_old_evidence(self):
        self.failures['management'] = '原失败项不重试'
        self.controls['management'] = dict(kind='stopped', reason='停止')
        self.run_queue()
        old = self.root / 'evidence/workbench_runs/run/workflow.json'
        original_bytes = old.read_bytes()
        self.hashes['daily_tasks.json'] = 'changed'
        self.calls.clear()
        with self.assertRaises(RuntimeError):
            self.run_queue(resume=True)
        plan = w.continuation_plan(old)
        self.assertEqual(plan['selected'], ['free_draw', 'guild'])
        w.save(self.manifest, plan)
        self.controls.clear()
        state = self.run_queue('continuation')
        self.assertEqual(self.calls, ['navigation', 'free_draw', 'guild'])
        self.assertEqual(old.read_bytes(), original_bytes)
        self.assertEqual(state['summary']['failed'], 1)
        self.assertEqual(state['status'], 'complete_with_errors')

    def test_final_summary_logged_once_and_reopen_can_read(self):
        self.failures['management'] = '错误A'
        state = self.run_queue()
        output = self.root / 'evidence/workbench_runs/run'
        log = ReceiptLog(output, 'workflow')
        lines = ''.join(e.line() for e in log.collect(final=True))
        self.assertIn('执行结束，有错误：成功 3，失败 1', lines)
        self.assertIn('错误A', lines)
        self.assertIn('运行记录：', lines)
        self.assertEqual(log.collect(final=True), [])
        self.assertIn('执行结束，有错误', ''.join(e.line() for e in ReceiptLog(output, 'workflow').collect(final=True)))

    def test_readonly_queue_is_never_business_completion(self):
        state = self.run_queue(execute=False)
        self.assertEqual(state['status'], 'inspected')
        self.assertFalse(state['completed'])


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'event.json'
        self.pending = dict(module='event', cache_date=game_day().isoformat(), event_id='event-test',
            mode='challenge', method='continuous', quantity=5, cost=5,
            status='submitted_pending_result', op_id='original-id', evidence='original-frame.png',
            input_receipt={'input_kind': 'left_click'})
        self.data = dict(schema_version=1, cache_date=game_day().isoformat(), event_id='event-test', pending=self.pending)
        w.save(self.path, self.data)

    def test_event_pending_preserved_for_independent_rewards_and_navigation(self):
        before = self.path.read_bytes()
        for module in ('navigation', 'goddess', 'rewards', 'task_rewards'):
            refs = guard_foreign_pending(self.root, self.root / 'selected.json', module=module)
            self.assertEqual(refs[0]['op_id'], 'original-id')
        self.assertEqual(self.path.read_bytes(), before)

    def test_shared_ap_or_default_caller_still_blocks(self):
        for module in (None, 'event', 'unknown'):
            with self.assertRaises(PendingConflictError):
                guard_foreign_pending(self.root, self.root / 'selected.json', module=module)

    def test_invalid_or_wrong_day_event_not_exempted(self):
        for key, value in (('op_id', None), ('cache_date', '2000-01-01'), ('input_receipt', None)):
            with self.subTest(key=key):
                data = copy.deepcopy(self.data)
                data['pending'][key] = value
                w.save(self.path, data)
                with self.assertRaises(PendingConflictError):
                    guard_foreign_pending(self.root, self.root / 'selected.json', module='task_rewards')

    def test_other_pending_still_blocks_and_corrupt_record_stops_globally(self):
        self.data['pending']['module'] = 'equipment'
        w.save(self.path, self.data)
        with self.assertRaises(PendingConflictError):
            guard_foreign_pending(self.root, self.root / 'selected.json', module='task_rewards')
        self.path.write_text('{', encoding='utf-8')
        with self.assertRaises(ExecutionControlError):
            guard_foreign_pending(self.root, self.root / 'selected.json', module='task_rewards')

    def test_control_signal_is_durable(self):
        control = self.root / 'control.json'
        with patch.dict(os.environ, BD2_CONTROL_FILE=str(control)):
            with self.assertRaises(ExecutionControlError):
                stop_execution('F8', 'stopped')
        self.assertEqual(json.loads(control.read_text(encoding='utf-8'))['kind'], 'stopped')


if __name__ == '__main__':
    unittest.main(verbosity=2)
