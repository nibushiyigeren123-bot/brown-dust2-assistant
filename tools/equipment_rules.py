"""Equipment selection and stop rules; no image claims or game input.

The UI adapter remains separate. Candidate fields must come from inspected UI,
not from a card's list index or a filter setting alone.
"""
import random
import re


def refine_candidates(cards):
    eligible = []
    for card in cards:
        badge = str(card.get('badge', '')).strip()
        if card.get('quality') != 'UR' or not re.fullmatch(r'\d+', badge):
            continue
        score = int(badge)
        if not 0 <= score < 24:
            continue
        eligible.append(card)
    return eligible


def choose_refine_candidate(cards, rng=None):
    candidates = refine_candidates(cards)
    return (rng or random.SystemRandom()).choice(candidates) if candidates else None


def decompose_candidate_allowed(card, selected_qualities, *, r_unavailable=False):
    # Read back the filter AND the chosen item's quality; neither is sufficient alone.
    quality = card.get('quality')
    return (list(selected_qualities) in (['R'], ['N']) and list(selected_qualities) == [quality]
            and (quality == 'R' or r_unavailable is True)
            and card.get('locked') is False and card.get('equipped') is False)


class EquipmentProgress:
    def __init__(self, state=None):
        self.state = state or {'refine_submitted': False, 'refine_completed': False,
                               'decompose_submitted': False, 'decompose_completed': False,
                               'filter_initialized': False}

    def submit_refine(self):
        if self.state['refine_submitted']:
            raise RuntimeError('The one refinement attempt is already submitted; reconcile it without retrying')
        self.state['refine_submitted'] = True

    def confirm_refine(self, before, after, cost):
        if not self.state['refine_submitted']:
            raise RuntimeError('No submitted refinement attempt')
        if any(type(values) is not int for values in (*before.values(), *after.values(), *cost.values())):
            raise ValueError('Refinement resource evidence must contain exact integer balances')
        if not cost or any(cost[k] <= 0 or before.get(k, -1)-after.get(k, -1) != cost[k] for k in cost):
            raise RuntimeError('Refinement consumption was not verified')
        # No score or success flag is required: the daily is one attempt.
        self.state['refine_completed'] = True

    def submit_decompose(self, card, selected_qualities, selected_count, *, r_unavailable=False):
        if not self.state['refine_completed']:
            raise RuntimeError('Complete one refinement attempt before decomposition')
        if self.state['decompose_submitted'] or type(selected_count) is not int or selected_count != 1:
            raise RuntimeError('Only one item may be submitted for decomposition')
        if not decompose_candidate_allowed(card, selected_qualities, r_unavailable=r_unavailable):
            raise RuntimeError('Select R alone; N alone requires verified absence of eligible R')
        self.state['decompose_submitted'] = True

    def confirm_decompose(self, items_removed):
        if not self.state['decompose_submitted'] or type(items_removed) is not int or items_removed != 1:
            raise RuntimeError('One-item decomposition result was not verified')
        self.state['decompose_completed'] = True

    def initialize_filter(self):
        if not self.state['decompose_completed'] or self.state['filter_initialized']:
            raise RuntimeError('Initialize the filter once, after verified decomposition')
        self.state['filter_initialized'] = True
