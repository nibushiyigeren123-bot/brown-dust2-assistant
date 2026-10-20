"""Pure controller for event battle batches and semantic UI transitions.

This module emits decisions but performs no I/O, OCR, input, or persistence.
The host must persist ``ledger`` after ``commit`` and before executing an action.
"""
from copy import deepcopy

from resource_pools import available_for_policy


MODES = ("normal", "challenge")
METHODS = ("continuous", "quick")
MAX_RUNS = 100


class EventBattleController:
    @classmethod
    def resume_pending(cls, config_event, ledger):
        """Restore result collection only; never reconstruct a submit decision."""
        from game_calendar import game_day
        pending = ledger.get('pending')
        if (not isinstance(pending, dict) or pending.get('module') != 'event'
                or pending.get('cache_date') != game_day().isoformat()
                or ledger.get('cache_date') != pending['cache_date']
                or ledger.get('event_id') != pending.get('event_id')
                or pending.get('method') not in METHODS or pending.get('mode') not in MODES
                or not pending.get('input_receipt') or not pending.get('op_id')
                or type(pending.get('free_only')) is not bool):
            raise RuntimeError('活动未决身份、日期或原点击回执不完整，保留原记录。')
        for key in ('start_stage', 'quantity', 'cost', 'unit_cost'):
            if type(pending.get(key)) is not int or pending[key] <= 0:
                raise RuntimeError('活动未决次数或费用无效，不恢复提交。')
        if (pending['quantity'] > MAX_RUNS or pending['cost'] != pending['quantity'] * pending['unit_cost']
                or type(pending.get('free_ap')) is not int or pending['free_ap'] < 0):
            raise RuntimeError('活动未决费用关系或原余额不完整。')
        event = deepcopy(config_event)
        event.update(event_id=pending['event_id'], mode=pending['mode'], battle_method=pending['method'],
                     enabled=True, runs=ledger.get('runs_by_mode', {}).get(pending['mode'], 0) + pending['quantity'],
                     use_owned_extra_ap=not pending['free_only'],
                     ap_budget=max(config_event.get('ap_budget', 0), ledger.get('spent_total', 0) + pending['cost']))
        controller = cls(event, ledger)
        controller._foreign_pending = False
        controller._owns_pending = True
        controller._bound_event_id = pending['event_id']
        controller._batch = deepcopy(pending)
        controller._phase = 'await_result'
        return controller

    def __init__(self, config_event, ledger):
        self.config = deepcopy(config_event)
        self.ledger = ledger
        self._last_obs = None
        self._await = None
        self._owns_pending = False
        self._foreign_pending = bool(ledger.get("pending"))
        self._bound_event_id = None
        self._phase = "navigate"
        self._main_seen = 0
        self._entered_event = False
        self._mode_selected = False
        self._method_opened = False
        self._toggle_attempted = False
        self._preparation = None
        self._quantity_steps = 0
        self._batch = None
        self._quick_reward_seen = False
        self._quick_dismiss_committed = False
        self._saw_final_victory = False
        self._preview_baseline = None
        self._prepared_only = False

    @staticmethod
    def _decision(status, *, action=None, point=None, reason=None, **extra):
        out = {"status": status, "action": action, "point": point}
        if reason is not None:
            out["reason"] = reason
        out.update(extra)
        return out

    def _wait(self, reason="等待新的识别帧"):
        return self._decision("wait", reason=reason)

    def _review(self, reason):
        return self._decision("needs_review", reason=reason)

    def _action(self, name, point=None, **extra):
        return self._decision("action", action=name, point=point, **extra)

    def _expected_event_id(self, obs):
        configured = self.config.get("event_id")
        observed = obs.get("event_id")
        if configured == "auto_current":
            expected = self._bound_event_id or (observed if isinstance(observed, str) and observed else None)
        else:
            expected = configured
        if expected is None:
            return None
        if observed is not None and observed != expected:
            return False
        return expected

    def _validate_configuration(self):
        c = self.config
        if type(c.get("enabled")) is not bool:
            return "活动 enabled 必须为布尔值。"
        if c.get("event_id") not in ("auto_current",) and not (
            isinstance(c.get("event_id"), str) and c.get("event_id")
        ):
            return "活动 event_id 缺失或无效。"
        if c.get("mode") not in MODES or c.get("battle_method") not in METHODS:
            return "活动模式或战斗方式无效。"
        for name in ("runs", "ap_budget"):
            if type(c.get(name)) is not int or c[name] < 0:
                return f"{name} 必须为非负整数。"
        if c["runs"] > MAX_RUNS:
            return f"单次配置超过安全上限 {MAX_RUNS} 次。"
        if type(c.get("use_owned_extra_ap")) is not bool:
            return "use_owned_extra_ap 必须为布尔值。"
        if "prepare_only" in c and type(c["prepare_only"]) is not bool:
            return "prepare_only 必须为布尔值。"
        if type(self.ledger.get("spent_total", 0)) is not int or self.ledger.get("spent_total", 0) < 0:
            return "活动共享 AP 账本无效。"
        runs = self.ledger.get("runs_by_mode", {})
        if any(type(runs.get(m, 0)) is not int or runs.get(m, 0) < 0 for m in MODES):
            return "按模式完成次数账本无效。"
        return None

    def _remaining(self):
        return self.config["runs"] - self.ledger.get("runs_by_mode", {}).get(self.config["mode"], 0)

    def _dialog_quantity(self, obs):
        if self.config["battle_method"] == "quick":
            value = obs.get("quantity")
            return value if type(value) is int and value > 0 else None
        prep = self._preparation or {}
        unit = prep.get("unit_cost")
        total = obs.get("total_cost")
        if type(unit) is not int or unit <= 0 or type(total) is not int or total <= 0 or total % unit:
            return None
        return total // unit

    def _await_satisfied(self, obs):
        """Require the action's semantic result, ignoring unrelated OCR fields."""
        pending = self._await
        if pending is None:
            return True
        action = pending["action"]
        state = obs.get("state")
        if action == "enter_event":
            return state == "event_hub"
        if action == "select_mode":
            return state == "event_preparation"
        if action in ("open_quick_dialog", "open_auto_dialog"):
            expected = "event_quick_dialog" if action == "open_quick_dialog" else "event_auto_dialog"
            return state == expected and obs.get("mode") == self.config["mode"]
        if action == "toggle_free_only":
            return (state in ("event_quick_dialog", "event_auto_dialog")
                    and obs.get("free_only") is pending["target_free_only"])
        if action in ("increment_quantity", "decrement_quantity"):
            return (state in ("event_quick_dialog", "event_auto_dialog")
                    and self._dialog_quantity(obs) == pending["expected_quantity"])
        if action == "submit_event_batch":
            accepted = (("event_reward", "event_defeat") if self._batch["method"] == "quick"
                        else ("event_auto_running", "event_victory", "event_defeat"))
            return state in accepted
        if action in ("dismiss_reward", "return_final", "cancel_event_preview"):
            return state == "event_preparation"
        if action == "back_to_event_hub":
            return state == "event_hub"
        if action == "back_to_main_menu":
            return state == "main_menu"
        return False

    def _matches_identity(self, obs, *, require_mode=False, require_event=False):
        event_id = self._expected_event_id(obs)
        if event_id is False:
            return False
        if require_event and (event_id is None or obs.get("event_id") is None):
            return None
        if require_mode and obs.get("mode") != self.config["mode"]:
            return False
        return True

    def _check_resources(self, free, owned, free_only, cost):
        expected_free_only = not self.config["use_owned_extra_ap"]
        if type(free_only) is not bool or free_only != expected_free_only:
            return None, "免费 AP 开关与配置不符或未识别。"
        available = available_for_policy(free, owned, free_only, cost)
        if available is None:
            return None, "免费/额外 AP 读数不足或未知，不能提交。"
        if available < cost:
            return None, "可用活动 AP 不足。"
        return available, None

    def _resource_delta_ok(self, before, after):
        cost = before["cost"]
        free_before, free_after = before.get("free_ap"), after.get("free_ap")
        extra_before, extra_after = before.get("owned_extra_ap"), after.get("owned_extra_ap")
        if type(free_before) is int and type(free_after) is int:
            free_delta = free_before - free_after
            if free_delta == cost:
                return True
            if (not before.get("free_only")
                    and type(extra_before) is int and type(extra_after) is int
                    and extra_before >= 0 and extra_after >= 0
                    and free_delta >= 0 and extra_before - extra_after >= 0
                    and free_delta + extra_before - extra_after == cost):
                return True
        return False

    def _prepare_batch(self, obs):
        if not self._mode_selected:
            return self._wait("等待活动中心确认所选战斗模式。")
        if not self._matches_identity(obs, require_mode=True):
            return self._review("活动或普通/挑战身份与当前配置不符。")
        stage, unit = obs.get("stage"), obs.get("unit_cost")
        if type(stage) is not int or stage <= 0:
            return self._wait("准备页关卡尚未可靠识别。")
        if type(unit) is not int or unit <= 0:
            return self._wait("单次 AP 费用尚未可靠识别。")
        remaining = self._remaining()
        if remaining <= 0:
            point = obs.get("points", {}).get("back")
            if point is None:
                return self._wait("准备页缺少返回控件位置。")
            return self._action("back_to_event_hub", point, transition="return_to_main")
        if self._method_opened:
            return self._wait("战斗设置已打开，等待对应设置对话框。")
        key = "open_quick" if self.config["battle_method"] == "quick" else "open_auto"
        point = obs.get("points", {}).get(key)
        if point is None:
            return self._wait(f"准备页缺少 {key} 控件位置。")
        return self._action("open_quick_dialog" if key == "open_quick" else "open_auto_dialog", point,
                            mode=self.config["mode"], stage=stage, unit_cost=unit)

    def _back_from_preparation(self, obs):
        point = obs.get("points", {}).get("back")
        if point is None:
            return self._wait("准备页缺少返回控件位置。")
        return self._action("back_to_event_hub", point)

    def _dialog_decision(self, obs):
        expected_state = "event_quick_dialog" if self.config["battle_method"] == "quick" else "event_auto_dialog"
        if obs.get("state") != expected_state:
            return self._wait("等待所选战斗方式的设置对话框。")
        if not self._method_opened:
            return self._wait("等待准备页打开指定战斗方式的设置。")
        if not self._matches_identity(obs, require_mode=True):
            return self._review("设置对话框的活动或战斗模式不符。")
        prep = self._preparation
        if not prep or obs.get("stage") != prep["stage"]:
            return self._review("设置对话框关卡与已确认准备页不一致。")
        if self.config["battle_method"] == "quick":
            quantity = obs.get("quantity")
            if type(quantity) is not int or quantity <= 0:
                return self._wait("快速战斗次数尚未可靠识别。")
        else:
            cost_now = obs.get("total_cost")
            unit = prep["unit_cost"]
            if type(cost_now) is not int or cost_now <= 0 or cost_now % unit:
                return self._wait("连续战斗总费用不能可靠换算为次数。")
            quantity = cost_now // unit
        if quantity > MAX_RUNS:
            return self._review(f"对话框次数超过 {MAX_RUNS} 次上限。")
        desired_free_only = not self.config["use_owned_extra_ap"]
        observed_free_only = obs.get("free_only")
        if type(observed_free_only) is not bool:
            return self._review("免费 AP 开关状态未知，不能安全切换或提交。")
        if observed_free_only != desired_free_only:
            if self._toggle_attempted:
                return self._review("免费 AP 开关在一次校正后仍不符合配置。")
            point = obs.get("points", {}).get("free_toggle")
            if point is None:
                return self._wait("免费 AP 开关与配置不符且缺少控件位置。")
            return self._action("toggle_free_only", point, target_free_only=desired_free_only)
        target = self._remaining()
        if target <= 0:
            return self._review("活动目标次数已完成，却仍停留在提交对话框。")
        if quantity != target:
            if self._quantity_steps >= MAX_RUNS:
                return self._review("调整次数超过 100 步上限。")
            if quantity < target:
                point = obs.get("points", {}).get("plus")
                if point is None:
                    return self._wait("次数不足且缺少加一控件位置。")
                return self._action("increment_quantity", point, target_quantity=target)
            point = obs.get("points", {}).get("minus")
            if point is None:
                return self._wait("次数过多且缺少减一控件位置。")
            return self._action("decrement_quantity", point, target_quantity=target)

        cost = obs.get("total_cost")
        if type(cost) is not int or cost <= 0 or cost != prep["unit_cost"] * quantity:
            return self._review("次数、单次费用与总 AP 费用不一致。")
        budget = self.config["ap_budget"]
        spent = self.ledger.get("spent_total", 0)
        if spent + cost > budget:
            return self._review("普通与挑战共享 AP 预算不足。")
        available, error = self._check_resources(obs.get("free_ap"), obs.get("owned_extra_ap"),
                                                  obs.get("free_only"), cost)
        if error:
            return self._review(error)
        point = obs.get("points", {}).get("submit")
        if point is None:
            return self._wait("设置对话框缺少唯一提交按钮位置。")
        batch = {
            "event_id": self._bound_event_id or self.config.get("event_id"),
            "mode": self.config["mode"],
            "method": self.config["battle_method"],
            "start_stage": prep["stage"],
            "quantity": quantity,
            "cost": cost,
            "unit_cost": prep["unit_cost"],
            "free_ap": obs["free_ap"],
            "owned_extra_ap": obs.get("owned_extra_ap"),
            "free_only": obs["free_only"],
            "available_ap": available,
        }
        if self.config.get("prepare_only", False):
            point = obs.get("points", {}).get("cancel")
            if point is None:
                return self._wait("设置预览缺少取消控件位置；不提交战斗。")
            return self._action("cancel_event_preview", point, batch=deepcopy(batch))
        return self._action("submit_event_batch", point, batch=deepcopy(batch))

    def _accept_pending(self, obs):
        batch = self._batch
        if batch is None:
            return self._review("本控制器没有已提交批次身份。")
        if not self._matches_identity(obs):
            return self._review("结算页面的活动身份与提交批次不符。")
        if batch["method"] == "quick":
            if obs["state"] == "event_reward":
                if obs.get("mode") not in (None, batch["mode"]) or obs.get("stage") not in (None, batch["start_stage"]):
                    return self._review("快速战斗奖励页身份与提交批次不符。")
                if not self._quick_reward_seen:
                    point = obs.get("points", {}).get("dismiss")
                    if point is None:
                        return self._wait("奖励页缺少经识别的返回控件位置。")
                    return self._action("dismiss_reward", point)
                return self._wait("奖励页已见并已安排一次返回，等待准备页。")
            if obs["state"] == "event_preparation":
                if not self._quick_reward_seen or not self._quick_dismiss_committed:
                    return self._review("快速战斗未经过奖励页和一次返回确认。")
                if (obs.get("mode") != batch["mode"] or obs.get("stage") != batch["start_stage"]):
                    return self._review("快速战斗返回后的模式或关卡不符。")
                if not self._resource_delta_ok(batch, obs):
                    return self._review("快速战斗 AP 差分与提交费用不符。")
                return self._settle_pending(obs)
            return self._wait("快速战斗等待直接奖励页。")

        state = obs["state"]
        if state == "event_defeat":
            return self._review("连续战斗失败；不自动重试或重发。")
        if state == "event_auto_running":
            progress = obs.get("auto_progress")
            if progress and (progress.get("total") != batch["quantity"]
                             or type(progress.get("current")) is not int
                             or not 1 <= progress["current"] <= batch["quantity"]):
                return self._review("连续战斗进度与提交次数不符。")
            return self._wait("连续战斗仍在进行。")
        if state == "event_victory":
            if obs.get("mode") != batch["mode"]:
                return self._review("连续战斗胜利页模式与批次不符。")
            stage = obs.get("stage")
            if type(stage) is not int or not batch["start_stage"] <= stage < batch["start_stage"] + batch["quantity"]:
                return self._review("连续战斗胜利关卡超出提交批次。")
            if stage != batch["start_stage"] + batch["quantity"] - 1:
                progress = obs.get("auto_progress")
                if progress and (progress.get("total") != batch["quantity"]
                                 or type(progress.get("current")) is not int
                                 or not 1 <= progress["current"] < batch["quantity"]):
                    return self._review("连续战斗中间进度与批次不符。")
                return self._wait("等待下一场连续战斗及最终胜利关。")
            progress = obs.get("auto_progress")
            if progress and (progress.get("total") != batch["quantity"]
                             or progress.get("current") != batch["quantity"]):
                return self._review("最后胜利页连续进度没有到达批次末次。")
            point = obs.get("points", {}).get("return")
            if point is None:
                return self._wait("最终胜利已见，等待返回准备页控件位置。")
            return self._action("return_final", point)
        if state == "event_preparation":
            if not self._saw_final_victory:
                return self._review("连续战斗返回准备页，但未观察到最终胜利关。")
            if obs.get("mode") != batch["mode"]:
                return self._review("连续战斗返回后的模式不符。")
            stage = obs.get("stage")
            normal_next = stage == batch["start_stage"] + batch["quantity"]
            terminal_same_stage = (stage == batch["start_stage"] + batch["quantity"] - 1
                                   and obs.get("points", {}).get("open_quick") is not None)
            if not (normal_next or terminal_same_stage):
                return self._review("连续战斗返回关卡未到预期下一关。")
            if not self._resource_delta_ok(batch, obs):
                return self._review("连续战斗 AP 差分与提交费用不符。")
            return self._settle_pending(obs)
        return self._wait("连续战斗等待胜利、运行进度或返回准备页。")

    def _settle_pending(self, obs):
        batch = self._batch
        summary = {key: batch[key] for key in
                   ("event_id", "mode", "method", "start_stage", "quantity", "cost")}
        return self._action("settle_event_batch", None, settlement=summary)

    def _return_flow(self, obs):
        state = obs.get("state")
        if state == "event_preparation":
            if self._phase == "preview_cancelled":
                baseline = self._preview_baseline or {}
                for key in ("free_ap", "owned_extra_ap"):
                    if obs.get(key) != baseline.get(key):
                        return self._review("预览取消后 AP 读数发生变化；暂停并核对。")
                if obs.get("mode") != baseline.get("mode") or obs.get("stage") != baseline.get("stage"):
                    return self._review("预览取消后模式或关卡与核对前不符。")
            return self._back_from_preparation(obs)
        if state == "event_hub":
            identity = self._matches_identity(obs, require_event=True)
            if identity is None:
                return self._wait("返回路径的活动 event_id 尚未可靠识别。")
            if not identity:
                return self._review("返回路径经过了其他活动。")
            point = obs.get("points", {}).get("back")
            if point is None:
                return self._wait("活动页缺少返回主页控件位置。")
            return self._action("back_to_main_menu", point)
        if state == "main_menu":
            self._main_seen += 1
            if self._remaining() <= 0 or self._prepared_only:
                if self._main_seen >= 2:
                    return self._decision("finished", reason="目标次数已核验并连续两帧返回主页。",
                                          prepared_only=self._prepared_only)
                return self._wait("已回到主页，等待第二次主页确认。")
            return self._wait("已回到主页，等待继续活动流程或确认其他运行计划。")
        self._main_seen = 0
        return self._wait("等待返回活动页或主页。")

    def step(self, obs):
        if not isinstance(obs, dict) or not isinstance(obs.get("state"), str):
            return self._wait("观察状态缺失或格式无效。")
        if self._foreign_pending:
            return self._review("账本已有未决消费；新控制器绝不重发，先核对原回执。")
        if self._owns_pending:
            if self._await is not None and not self._await_satisfied(obs):
                self._last_obs = deepcopy(obs)
                return self._wait("提交已落账，等待与动作对应的战斗结果；不会重发。")
            self._await = None
            self._last_obs = deepcopy(obs)
            if not self.ledger.get("pending"):
                return self._review("内部状态显示已有批次但账本 pending 丢失。")
            return self._accept_pending(obs)

        config_error = self._validate_configuration()
        if config_error:
            return self._review(config_error)
        if not self.config["enabled"]:
            if obs["state"] == "main_menu":
                return self._decision("finished", reason="活动已停用；跳过且未消费。", skipped=True)
            return self._wait("活动停用；仅在主页报告跳过。")
        self._last_obs = deepcopy(obs)

        if self._await is not None:
            action = self._await["action"]
            if action == "submit_event_batch":
                return self._review("已提交动作无待决记录，禁止重复提交。")
            if not self._await_satisfied(obs):
                return self._wait("动作仍未满足对应界面后置条件；不重复发送。")
            self._await = None

        state = obs["state"]
        if self._phase in ("return_to_main", "preview_cancelled"):
            return self._return_flow(obs)

        if state == "main_menu":
            self._main_seen = 0
            if self._remaining() <= 0:
                return self._decision("finished", reason="配置目标次数已完成，无需重复执行。")
            identity = self._matches_identity(obs, require_event=True)
            if identity is None:
                return self._wait("主页当前活动 event_id 尚未可靠识别。")
            if not identity:
                return self._review("主页活动身份与当前配置不符。")
            point = obs.get("points", {}).get("enter_event")
            if point is None:
                return self._wait("主页缺少活动入口点。")
            return self._action("enter_event", point, event_id=obs.get("event_id"))

        if state == "event_hub":
            if not self._entered_event:
                return self._wait("必须从主页活动入口进入后才接受活动中心状态。")
            if self._mode_selected:
                return self._wait("已选择战斗模式，等待对应准备页。")
            identity = self._matches_identity(obs, require_event=True)
            if identity is None:
                return self._wait("活动中心 event_id 尚未可靠识别。")
            if not identity:
                return self._review("活动中心身份与配置不符。")
            mode_point = obs.get("points", {}).get(self.config["mode"])
            if mode_point is None:
                return self._wait("活动中心缺少当前模式入口点。")
            return self._action("select_mode", mode_point, mode=self.config["mode"])

        if state == "event_preparation":
            return self._prepare_batch(obs)

        if state in ("event_auto_dialog", "event_quick_dialog"):
            return self._dialog_decision(obs)

        return self._wait("当前页面尚无可执行活动动作，等待新识别帧。")

    def commit(self, decision):
        """Record an emitted action before the host performs any input."""
        if not isinstance(decision, dict) or decision.get("status") != "action":
            raise ValueError("commit 只接受本控制器返回的 action 决策。")
        action = decision.get("action")
        allowed = {"enter_event", "select_mode", "open_quick_dialog", "open_auto_dialog",
                   "toggle_free_only", "increment_quantity", "decrement_quantity",
                   "submit_event_batch", "cancel_event_preview", "dismiss_reward", "return_final", "settle_event_batch",
                   "back_to_event_hub", "back_to_main_menu"}
        if action not in allowed:
            raise ValueError("未知活动 action。")
        if self._await is not None and action != "settle_event_batch":
            raise RuntimeError("上一动作仍在等待识别回执，不能重复 commit。")
        if action == "submit_event_batch":
            if self.ledger.get("pending"):
                raise RuntimeError("提交决策无效或已存在 pending；禁止再次提交。")
            batch = decision["batch"]
            self._batch = deepcopy(batch)
            self.ledger["pending"] = {
                "event_id": batch["event_id"], "mode": batch["mode"], "method": batch["method"],
                "module": "event",
                "start_stage": batch["start_stage"], "quantity": batch["quantity"], "cost": batch["cost"],
                "unit_cost": batch["unit_cost"], "free_ap": batch["free_ap"],
                "owned_extra_ap": batch["owned_extra_ap"], "free_only": batch["free_only"],
                "status": "submitted_pending_result",
            }
            self._owns_pending = True
            self._phase = "await_result"
            self._quick_reward_seen = False
            self._quick_dismiss_committed = False
            self._saw_final_victory = False
            self._await = {"action": action, "before": deepcopy(self._last_obs)}
            return
        if action == "cancel_event_preview":
            if self.config.get("prepare_only") is not True or self._owns_pending or self.ledger.get("pending"):
                raise RuntimeError("仅 prepare_only 模式可取消预览；账本存在待决时不允许。")
            batch = decision.get("batch")
            if not isinstance(batch, dict):
                raise ValueError("预览取消决策缺少已核对设置摘要。")
            self._preview_baseline = {
                "free_ap": batch.get("free_ap"),
                "owned_extra_ap": batch.get("owned_extra_ap"),
                "mode": batch.get("mode"),
                "stage": batch.get("start_stage"),
            }
            self._prepared_only = True
            self._phase = "preview_cancelled"
            self._await = {"action": action, "before": deepcopy(self._last_obs)}
            return
        if action == "settle_event_batch":
            batch = self._batch
            pending = self.ledger.get("pending")
            if not self._owns_pending or not batch or not isinstance(pending, dict):
                raise RuntimeError("没有本控制器已核验的待结算批次。")
            if pending.get("module") != "event":
                raise RuntimeError("账本 pending 不属于 event 模块；不结算。")
            expected = {key: batch[key] for key in
                        ("event_id", "mode", "method", "start_stage", "quantity", "cost")}
            if decision.get("settlement") != expected:
                raise ValueError("结算决策与 pending 批次不一致。")
            if any(pending.get(key) != batch.get(key) for key in
                   ("event_id", "mode", "method", "start_stage", "quantity", "cost")):
                raise RuntimeError("账本 pending 与本控制器批次摘要不一致；不结算。")
            self.ledger["spent_total"] = self.ledger.get("spent_total", 0) + batch["cost"]
            runs = self.ledger.setdefault("runs_by_mode", {"normal": 0, "challenge": 0})
            for mode in MODES:
                runs.setdefault(mode, 0)
            runs[batch["mode"]] += batch["quantity"]
            self.ledger.setdefault("batches", []).append({**expected, 'submission': deepcopy(pending)})
            self.ledger["pending"] = None
            self._owns_pending = False
            self._phase = "return_to_main"
            self._await = None
            return
        if action == "dismiss_reward":
            if (not self._owns_pending or not self._batch
                    or self._batch["method"] != "quick" or self._quick_dismiss_committed):
                raise RuntimeError("快速奖励页返回动作不是唯一允许的返回。")
            self._quick_reward_seen = True
            self._quick_dismiss_committed = True
        if action == "back_to_event_hub" and self._phase == "preview_cancelled":
            self._phase = "return_to_main"
        if action == "enter_event":
            self._entered_event = True
            event_id = decision.get("event_id")
            if isinstance(event_id, str) and event_id:
                self._bound_event_id = event_id
        elif action == "select_mode":
            self._mode_selected = True
        elif action in ("open_auto_dialog", "open_quick_dialog"):
            self._method_opened = True
            self._preparation = {"mode": decision.get("mode"), "stage": decision.get("stage"),
                                 "unit_cost": decision.get("unit_cost")}
            self._toggle_attempted = False
            self._quantity_steps = 0
        elif action in ("increment_quantity", "decrement_quantity"):
            quantity = self._dialog_quantity(self._last_obs or {})
            if quantity is None:
                raise RuntimeError("次数调整动作缺少可核验的提交前次数。")
            self._quantity_steps += 1
            self._await = {"action": action, "before": deepcopy(self._last_obs),
                           "expected_quantity": quantity + (1 if action == "increment_quantity" else -1)}
            return
        elif action == "toggle_free_only":
            self._await = {"action": action, "before": deepcopy(self._last_obs),
                           "target_free_only": decision.get("target_free_only")}
            self._toggle_attempted = True
            return
        elif action == "return_final":
            self._saw_final_victory = True
        if decision.get("transition") == "return_to_main":
            self._phase = "return_to_main"
        self._await = {"action": action, "before": deepcopy(self._last_obs)}


__all__ = ["EventBattleController"]
