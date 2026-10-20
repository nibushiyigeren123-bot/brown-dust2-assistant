"""Offline boundary checks for event_battle_flow; no game or file I/O."""
import sys
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from event_battle_flow import EventBattleController  # noqa: E402


def config(*, mode="normal", method="quick", runs=1, budget=10, extra=False):
    return {
        "event_id": "Loyal_Maid_20261016",
        "enabled": True,
        "mode": mode,
        "battle_method": method,
        "runs": runs,
        "ap_budget": budget,
        "use_owned_extra_ap": extra,
    }


def ledger(*, pending=None, spent=0, normal=0, challenge=0):
    return {
        "pending": pending,
        "spent_total": spent,
        "runs_by_mode": {"normal": normal, "challenge": challenge},
        "batches": [],
    }


def obs(state, **values):
    return {"state": state, **values}


def navigate_to_dialog(controller, mode="normal", method="quick", stage=15, unit=1):
    home = obs("main_menu", event_id="Loyal_Maid_20261016", points={"enter_event": [1, 1]})
    decision = controller.step(home)
    assert decision["action"] == "enter_event"
    controller.commit(decision)
    hub = obs("event_hub", event_id="Loyal_Maid_20261016",
              points={mode: [2, 2], "back": [3, 3]})
    decision = controller.step(hub)
    assert decision["action"] == "select_mode"
    controller.commit(decision)
    prep = obs("event_preparation", mode=mode, stage=stage, unit_cost=unit,
               free_ap=5, owned_extra_ap=None,
               points={"open_quick" if method == "quick" else "open_auto": [4, 4], "back": [3, 3]})
    decision = controller.step(prep)
    assert decision["action"] == ("open_quick_dialog" if method == "quick" else "open_auto_dialog")
    controller.commit(decision)
    return prep


def quick_dialog(*, mode="normal", stage=15, quantity=1, cost=1, free=5, extra=None,
                 free_only=True):
    return obs("event_quick_dialog", mode=mode, stage=stage, quantity=quantity,
               total_cost=cost, free_only=free_only, free_ap=free, owned_extra_ap=extra,
               points={"min": [1, 1], "plus": [2, 2], "minus": [3, 3],
                       "free_toggle": [4, 4], "submit": [5, 5], "cancel": [6, 6]})


class CandidateChecks(unittest.TestCase):
    def test_shared_budget_rejects_second_mode_spend(self):
        c = EventBattleController(config(mode="challenge", budget=5), ledger(spent=4))
        navigate_to_dialog(c, mode="challenge", stage=1, unit=2)
        decision = c.step(quick_dialog(mode="challenge", stage=1, quantity=1, cost=2, free=5))
        self.assertEqual(decision["status"], "needs_review")
        self.assertIn("共享 AP 预算", decision["reason"])

    def test_pending_is_written_before_submit_and_new_controller_never_resubmits(self):
        original = ledger()
        c = EventBattleController(config(), original)
        navigate_to_dialog(c)
        decision = c.step(quick_dialog())
        self.assertEqual(decision["action"], "submit_event_batch")
        self.assertIsNone(original["pending"])
        c.commit(decision)
        self.assertEqual(original["pending"]["status"], "submitted_pending_result")
        self.assertEqual(original["pending"]["module"], "event")
        restarted = EventBattleController(config(), original)
        self.assertEqual(restarted.step(quick_dialog())["status"], "needs_review")

    def test_free_only_and_extra_pool_gate(self):
        c = EventBattleController(config(extra=False), ledger())
        navigate_to_dialog(c)
        d = c.step(quick_dialog(free=5, free_only=True))
        self.assertEqual(d["action"], "submit_event_batch")

        c2 = EventBattleController(config(extra=True), ledger())
        navigate_to_dialog(c2)
        d = c2.step(quick_dialog(free=0, extra=10, free_only=True))
        self.assertEqual(d["action"], "toggle_free_only")
        c2.commit(d)
        self.assertEqual(c2.step(quick_dialog(free=0, extra=10, free_only=False))["action"],
                         "submit_event_batch")

        c3 = EventBattleController(config(extra=True), ledger())
        navigate_to_dialog(c3)
        d = c3.step(quick_dialog(free=0, extra=None, free_only=False))
        self.assertEqual(d["status"], "needs_review")
        self.assertIn("AP", d["reason"])

    def test_quick_reward_single_dismiss_and_resource_delta(self):
        book = ledger()
        c = EventBattleController(config(), book)
        navigate_to_dialog(c)
        submit = c.step(quick_dialog())
        c.commit(submit)
        self.assertEqual(c.step(quick_dialog())["status"], "wait")
        reward = obs("event_reward", mode="normal", stage=15, points={"dismiss": [7, 7]})
        dismiss = c.step(reward)
        self.assertEqual(dismiss["action"], "dismiss_reward")
        c.commit(dismiss)
        self.assertEqual(c.step(reward)["status"], "wait")
        prep = obs("event_preparation", mode="normal", stage=15, free_ap=4,
                   owned_extra_ap=None, points={"back": [3, 3]})
        settlement = c.step(prep)
        self.assertEqual(settlement["action"], "settle_event_batch")
        self.assertEqual(c.step(prep), settlement)  # two-frame decision is stable before commit
        self.assertEqual(book["spent_total"], 0)
        self.assertIsNotNone(book["pending"])
        c.commit(settlement)
        self.assertIsNone(book["pending"])
        back = c.step(prep)
        self.assertEqual(back["action"], "back_to_event_hub")
        self.assertIsNone(book["pending"])
        self.assertEqual(book["spent_total"], 1)
        self.assertEqual(book["runs_by_mode"]["normal"], 1)
        c.commit(back)
        hub = obs("event_hub", event_id="Loyal_Maid_20261016", points={"back": [3, 3]})
        home_back = c.step(hub)
        self.assertEqual(home_back["action"], "back_to_main_menu")
        c.commit(home_back)
        self.assertEqual(c.step(obs("main_menu", event_id="Loyal_Maid_20261016"))["status"], "wait")
        self.assertEqual(c.step(obs("main_menu", event_id="Loyal_Maid_20261016"))["status"], "finished")

        bad_book = ledger()
        bad = EventBattleController(config(), bad_book)
        navigate_to_dialog(bad)
        bad.commit(bad.step(quick_dialog()))
        bad.step(obs("event_reward", points={"dismiss": [7, 7]}))
        bad.commit({"status": "action", "action": "dismiss_reward", "point": [7, 7]})
        result = bad.step(obs("event_preparation", mode="normal", stage=15, free_ap=5,
                              points={"back": [3, 3]}))
        self.assertEqual(result["status"], "needs_review")
        self.assertIsNotNone(bad_book["pending"])

    def test_settlement_rejects_tampered_pending_without_mutating_ledger(self):
        book = ledger()
        c = EventBattleController(config(), book)
        navigate_to_dialog(c)
        c.commit(c.step(quick_dialog()))
        c.step(obs("event_reward", mode="normal", stage=15, points={"dismiss": [7, 7]}))
        c.commit(c.step(obs("event_reward", mode="normal", stage=15, points={"dismiss": [7, 7]})))
        prep = obs("event_preparation", mode="normal", stage=15, free_ap=4,
                   owned_extra_ap=None, points={"back": [3, 3]})
        settlement = c.step(prep)
        book["pending"]["cost"] = 9
        with self.assertRaises(RuntimeError):
            c.commit(settlement)
        self.assertEqual(book["spent_total"], 0)
        self.assertEqual(book["runs_by_mode"]["normal"], 0)
        self.assertIsNotNone(book["pending"])

    def test_continuous_final_victory_and_progress_allow_missing_intermediate_frame(self):
        book = ledger()
        c = EventBattleController(config(method="continuous", runs=2), book)
        navigate_to_dialog(c, method="continuous", stage=10, unit=1)
        dialog = obs("event_auto_dialog", mode="normal", stage=10, total_cost=2,
                     free_only=True, free_ap=5, owned_extra_ap=None,
                     points={"min": [1, 1], "plus": [2, 2], "minus": [3, 3],
                             "free_toggle": [4, 4], "submit": [5, 5], "cancel": [6, 6]})
        submit = c.step(dialog)
        self.assertEqual(submit["action"], "submit_event_batch")
        c.commit(submit)
        running = obs("event_auto_running", auto_progress={"current": 2, "total": 2})
        self.assertEqual(c.step(running)["status"], "wait")
        victory = obs("event_victory", mode="normal", stage=11,
                      auto_progress={"current": 2, "total": 2}, points={"return": [8, 8]})
        ret = c.step(victory)
        self.assertEqual(ret["action"], "return_final")
        c.commit(ret)
        prep = obs("event_preparation", mode="normal", stage=12, free_ap=3,
                   owned_extra_ap=None, points={"back": [3, 3]})
        settlement = c.step(prep)
        self.assertEqual(settlement["action"], "settle_event_batch")
        self.assertEqual(book["spent_total"], 0)
        c.commit(settlement)
        back = c.step(prep)
        self.assertEqual(back["action"], "back_to_event_hub")
        self.assertEqual(book["runs_by_mode"]["normal"], 2)
        self.assertEqual(book["spent_total"], 2)

    def test_wrong_identity_and_count_cost_mismatch_block_submit(self):
        c = EventBattleController(config(), ledger())
        self.assertEqual(c.step(obs("main_menu", event_id="OtherEvent", points={"enter_event": [1, 1]}))["status"],
                         "needs_review")

        c2 = EventBattleController(config(runs=2), ledger())
        navigate_to_dialog(c2)
        d = c2.step(quick_dialog(quantity=2, cost=1, free=5))
        self.assertEqual(d["status"], "needs_review")
        self.assertIn("不一致", d["reason"])

    def test_auto_quantity_is_cost_divided_by_unit_and_not_stage_number(self):
        c = EventBattleController(config(method="continuous", runs=2), ledger())
        navigate_to_dialog(c, method="continuous", stage=15, unit=2)
        d = c.step(obs("event_auto_dialog", mode="normal", stage=15, total_cost=4,
                       free_only=True, free_ap=10, owned_extra_ap=None,
                       points={"min": [1, 1], "plus": [2, 2], "minus": [3, 3],
                               "free_toggle": [4, 4], "submit": [5, 5]}))
        self.assertEqual(d["action"], "submit_event_batch")
        self.assertEqual(d["batch"]["quantity"], 2)
        self.assertEqual(d["batch"]["start_stage"], 15)

    def test_quantity_one_to_five_uses_confirmed_single_steps_without_min(self):
        c = EventBattleController(config(runs=5), ledger())
        navigate_to_dialog(c)
        current = quick_dialog(quantity=1, cost=1)
        actions = []
        for quantity in range(1, 5):
            decision = c.step(current)
            self.assertEqual(decision["action"], "increment_quantity")
            self.assertEqual(decision["target_quantity"], 5)
            actions.append(decision["action"])
            c.commit(decision)
            self.assertEqual(c.step(current)["status"], "wait")
            noisy = {**current, "ocr_score": 0.97, "confidence_debug": {"token": quantity}}
            self.assertEqual(c.step(noisy)["status"], "wait")
            current = quick_dialog(quantity=quantity + 1, cost=quantity + 1)
        decision = c.step(current)
        self.assertEqual(decision["action"], "submit_event_batch")
        self.assertEqual(actions, ["increment_quantity"] * 4)
        self.assertEqual(decision["batch"]["quantity"], 5)

    def test_delayed_toggle_waits_through_identical_and_score_only_observations(self):
        c = EventBattleController(config(extra=True), ledger())
        navigate_to_dialog(c)
        before = quick_dialog(free=0, extra=10, free_only=True)
        toggle = c.step(before)
        self.assertEqual(toggle["action"], "toggle_free_only")
        c.commit(toggle)
        self.assertEqual(c.step(before)["status"], "wait")
        self.assertEqual(c.step({**before, "ocr_score": 1.0})["status"], "wait")
        after = quick_dialog(free=0, extra=10, free_only=False)
        self.assertEqual(c.step(after)["action"], "submit_event_batch")

    def test_final_victory_action_is_not_reissued_for_same_screen_noise(self):
        c = EventBattleController(config(method="continuous"), ledger())
        navigate_to_dialog(c, method="continuous")
        dialog = obs("event_auto_dialog", mode="normal", stage=15, total_cost=1,
                     free_only=True, free_ap=5, owned_extra_ap=None,
                     points={"min": [1, 1], "plus": [2, 2], "minus": [3, 3],
                             "free_toggle": [4, 4], "submit": [5, 5]})
        c.commit(c.step(dialog))
        victory = obs("event_victory", mode="normal", stage=15,
                      auto_progress={"current": 1, "total": 1}, points={"return": [8, 8]})
        ret = c.step(victory)
        self.assertEqual(ret["action"], "return_final")
        c.commit(ret)
        self.assertEqual(c.step(victory)["status"], "wait")
        self.assertEqual(c.step({**victory, "ocr_score": 0.88})["status"], "wait")
        prep = obs("event_preparation", mode="normal", stage=16, free_ap=4,
                   owned_extra_ap=None, points={"back": [3, 3]})
        self.assertEqual(c.step(prep)["action"], "settle_event_batch")

    def test_prepare_only_cancels_without_pending_or_consumption_then_returns_home(self):
        book = ledger()
        c = EventBattleController({**config(), "prepare_only": True}, book)
        navigate_to_dialog(c)
        dialog = quick_dialog()
        cancel = c.step(dialog)
        self.assertEqual(cancel["action"], "cancel_event_preview")
        self.assertEqual(c.step(dialog), cancel)
        self.assertIsNone(book["pending"])
        self.assertEqual(book["spent_total"], 0)
        self.assertEqual(book["runs_by_mode"]["normal"], 0)
        c.commit(cancel)
        self.assertEqual(c.step(dialog)["status"], "wait")
        prep = obs("event_preparation", mode="normal", stage=15, free_ap=5,
                   owned_extra_ap=None, points={"back": [3, 3]})
        back = c.step(prep)
        self.assertEqual(back["action"], "back_to_event_hub")
        c.commit(back)
        hub = obs("event_hub", event_id="Loyal_Maid_20261016", points={"back": [3, 3]})
        home_back = c.step(hub)
        self.assertEqual(home_back["action"], "back_to_main_menu")
        c.commit(home_back)
        home = obs("main_menu", event_id="Loyal_Maid_20261016")
        self.assertEqual(c.step(home)["status"], "wait")
        finished = c.step(home)
        self.assertEqual(finished["status"], "finished")
        self.assertTrue(finished["prepared_only"])
        self.assertIsNone(book["pending"])
        self.assertEqual(book["spent_total"], 0)
        self.assertEqual(book["runs_by_mode"]["normal"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
