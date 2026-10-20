"""Offline recovery checks for event batches; no game, runtime, or project writes."""
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from event_battle_flow import EventBattleController  # noqa: E402


GAME_DAY = "2026-10-20"
EVENT_ID = "Loyal_Maid_20261016"


def make_config(**changes):
    value = {
        "event_id": EVENT_ID,
        "enabled": True,
        "mode": "normal",
        "battle_method": "quick",
        "runs": 1,
        "ap_budget": 10,
        "use_owned_extra_ap": False,
    }
    value.update(changes)
    return value


def make_ledger(pending=None):
    return {
        "cache_date": GAME_DAY,
        "event_id": EVENT_ID,
        "spent_total": 0,
        "runs_by_mode": {"normal": 0, "challenge": 0},
        "batches": [],
        "pending": pending,
    }


def complete_pending(**changes):
    value = {
        "event_id": EVENT_ID,
        "mode": "normal",
        "method": "quick",
        "module": "event",
        "start_stage": 15,
        "quantity": 1,
        "cost": 1,
        "unit_cost": 1,
        "free_ap": 5,
        "owned_extra_ap": None,
        "free_only": True,
        "status": "submitted_pending_result",
        "cache_date": GAME_DAY,
        "op_id": "offline-op-001",
        "input_receipt": {"sent": True, "press_ms": 250},
    }
    value.update(changes)
    return value


def observation(state, **values):
    return {"state": state, **values}


class EventPendingRecoveryChecks(unittest.TestCase):
    def setUp(self):
        self.day_patch = patch("game_calendar.game_day")
        self.day_patch.start().return_value = __import__("datetime").date.fromisoformat(GAME_DAY)

    def tearDown(self):
        self.day_patch.stop()

    def make_submitted_controller(self):
        book = make_ledger()
        controller = EventBattleController(make_config(), book)

        home = observation("main_menu", event_id=EVENT_ID,
                           points={"enter_event": [1, 1]})
        decision = controller.step(home)
        self.assertEqual(decision["action"], "enter_event")
        controller.commit(decision)

        hub = observation("event_hub", event_id=EVENT_ID,
                          points={"normal": [2, 2]})
        decision = controller.step(hub)
        self.assertEqual(decision["action"], "select_mode")
        controller.commit(decision)

        prep = observation("event_preparation", mode="normal", stage=15,
                           unit_cost=1, free_ap=5, owned_extra_ap=None,
                           points={"open_quick": [3, 3]})
        decision = controller.step(prep)
        self.assertEqual(decision["action"], "open_quick_dialog")
        controller.commit(decision)

        dialog = observation("event_quick_dialog", mode="normal", stage=15,
                             quantity=1, total_cost=1, free_only=True,
                             free_ap=5, owned_extra_ap=None,
                             points={"submit": [4, 4]})
        decision = controller.step(dialog)
        self.assertEqual(decision["action"], "submit_event_batch")
        controller.commit(decision)
        pending = book["pending"]
        pending.update(cache_date=GAME_DAY, op_id="offline-op-001",
                       input_receipt={"sent": True, "press_ms": 250})
        return controller, book

    def test_old_preparation_frame_waits_for_submit_result_and_keeps_pending(self):
        controller, book = self.make_submitted_controller()
        transient = observation("event_preparation", mode="normal", stage=15,
                               free_ap=5, owned_extra_ap=None,
                               points={"open_quick": [3, 3]})
        decision = controller.step(transient)
        self.assertEqual(decision["status"], "wait")
        self.assertNotEqual(decision.get("action"), "submit_event_batch")
        self.assertEqual(book["pending"]["free_ap"], 5)
        self.assertEqual(book["spent_total"], 0)

    def test_resume_uses_original_batch_even_when_current_config_changed(self):
        pending = complete_pending()
        book = make_ledger(pending)
        changed_config = make_config(enabled=False, event_id="DifferentEvent",
                                     mode="challenge", battle_method="continuous",
                                     runs=0, ap_budget=0, use_owned_extra_ap=True)
        resumed = EventBattleController.resume_pending(changed_config, book)

        reward = observation("event_reward", mode="normal", stage=15,
                             points={"dismiss": [5, 5]})
        dismiss = resumed.step(reward)
        self.assertEqual(dismiss["action"], "dismiss_reward")
        self.assertEqual(dismiss["point"], [5, 5])
        resumed.commit(dismiss)

        prep = observation("event_preparation", mode="normal", stage=15,
                           free_ap=4, owned_extra_ap=None,
                           points={"back": [6, 6]})
        settle = resumed.step(prep)
        self.assertEqual(settle["action"], "settle_event_batch")
        self.assertEqual(settle["settlement"]["event_id"], EVENT_ID)
        self.assertEqual(settle["settlement"]["mode"], "normal")
        self.assertEqual(settle["settlement"]["quantity"], 1)
        self.assertEqual(settle["settlement"]["cost"], 1)
        self.assertNotEqual(settle.get("action"), "submit_event_batch")
        resumed.commit(settle)
        self.assertIsNone(book["pending"])
        self.assertEqual(book["spent_total"], 1)
        self.assertEqual(book["runs_by_mode"]["normal"], 1)

        back = resumed.step(prep)
        self.assertEqual(back["action"], "back_to_event_hub")
        resumed.commit(back)
        hub = observation("event_hub", event_id=EVENT_ID,
                          points={"back": [7, 7]})
        home = resumed.step(hub)
        self.assertEqual(home["action"], "back_to_main_menu")
        resumed.commit(home)
        self.assertEqual(resumed.step(observation("main_menu", event_id=EVENT_ID))["status"], "wait")
        self.assertEqual(resumed.step(observation("main_menu", event_id=EVENT_ID))["status"], "finished")
        self.assertEqual(book["spent_total"], 1)
        self.assertEqual(book["runs_by_mode"]["normal"], 1)

    def test_resume_rejects_foreign_module_wrong_date_and_bad_cost_relation(self):
        cases = (
            ("foreign module", complete_pending(module="guild")),
            ("old pending date", complete_pending(cache_date="2026-10-19")),
            ("old ledger date", complete_pending(), GAME_DAY, "2026-10-19"),
            ("cost relation", complete_pending(cost=2)),
        )
        for item in cases:
            with self.subTest(case=item[0]):
                _, pending = item[:2]
                ledger_date = item[3] if len(item) > 3 else (item[2] if len(item) > 2 else GAME_DAY)
                book = make_ledger(deepcopy(pending))
                book["cache_date"] = ledger_date
                before = deepcopy(book)
                with self.assertRaises(RuntimeError):
                    EventBattleController.resume_pending(make_config(), book)
                self.assertEqual(book, before)

    def test_bad_ap_delta_after_reward_dismissal_preserves_pending(self):
        pending = complete_pending()
        book = make_ledger(pending)
        before = deepcopy(book)
        resumed = EventBattleController.resume_pending(make_config(), book)
        reward = observation("event_reward", mode="normal", stage=15,
                             points={"dismiss": [5, 5]})
        dismissal = resumed.step(reward)
        self.assertEqual(dismissal["action"], "dismiss_reward")
        resumed.commit(dismissal)

        # The AP balance is unchanged from the original pre-submit reading.
        prep = observation("event_preparation", mode="normal", stage=15,
                           free_ap=5, owned_extra_ap=None,
                           points={"back": [6, 6]})
        decision = resumed.step(prep)
        self.assertEqual(decision["status"], "needs_review")
        self.assertNotEqual(decision.get("action"), "submit_event_batch")
        self.assertEqual(book["pending"], before["pending"])
        self.assertEqual(book["spent_total"], 0)
        self.assertEqual(book["runs_by_mode"], before["runs_by_mode"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
