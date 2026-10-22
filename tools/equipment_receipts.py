"""Image-backed receipts for one R item when the inventory OCR is unavailable."""
import hashlib
from pathlib import Path

import cv2
import numpy as np

from m00_recognize import read_image


def image_receipt(path):
    from frame_store import persist
    path = persist(path).resolve(strict=True)
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def receipt_image(receipt):
    path = Path(receipt['path'])
    if hashlib.sha256(path.read_bytes()).hexdigest() != receipt['sha256']:
        raise ValueError('Equipment evidence changed')
    return read_image(path)


def normalized(image):
    return cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)


def same_item(first, second, card):
    """Compare the inspected art before selection changes its appearance."""
    x, y = 480 + 84 * card['column'], 97 + 84 * card['row']
    a = normalized(first)[y+23:y+68, x+7:x+46]
    b = normalized(second)[y+23:y+68, x+7:x+46]
    return bool(a.shape == b.shape and float(cv2.absdiff(a, b).mean()) < 8
                and float(cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)[0, 0]) >= .98)


def tight_anchor(vision, image, name, left, right):
    """Match the digit AND its following separator/unit, not a whole line."""
    a, b, c, d = vision.specs[name]['box']
    template = vision.images[name][:, left:right]
    region = normalized(image)[b-2:d+2, a+left-2:a+right+2]
    return float(cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED).max()) >= .97


def exactly_one_selected(vision, image):
    return (vision.anchor(image, 'decompose_select_prompt', .97)
            and vision.anchor(image, 'selected_count_one', .97)
            and tight_anchor(vision, image, 'selected_count_one', 0, 23))


def exactly_one_confirmed(vision, image):
    return (all(vision.anchor(image, name, .97) for name in
                ('decompose_confirm_warning', 'decompose_confirm_button'))
            and tight_anchor(vision, image, 'decompose_confirm_one', 0, 96)
            and tight_anchor(vision, image, 'decompose_confirm_one', 29, 69))


def selected_slot(vision, image, card):
    # Only the outer golden frame is shared between different equipment art.
    # The quality glyph remains independently required by visible_cards.
    from equipment_workflow import visible_cards
    if not any(c['point'] == card['point'] for c in visible_cards(image, vision, card['quality'])):
        return False
    frame = normalized(image)
    template = vision.images['selected_r_gun']
    mask = np.zeros(template.shape[:2], dtype=np.uint8)
    mask[3:76, :3] = 255
    mask[3:76, -3:] = 255
    mask[:3, 5:60] = 255
    # Absolute difference also rejects a similarly shaped unselected grey frame.
    selected = []
    for row in range(6):
        for column in range(8):
            x, y = 480 + 84 * column, 97 + 84 * row
            patch = frame[y-1:y+80, x-1:x+80]
            if (patch.shape == template.shape
                    and float(cv2.absdiff(patch, template)[mask > 0].mean()) < 18):
                selected.append((row, column))
    return selected == [(card['row'], card['column'])]


def verify_selection(vision, observation, selection):
    from equipment_workflow import visible_cards
    proof = observation['visual_receipt']
    card = proof['card']
    if card.get('quality') != 'R' or observation['selected_qualities'] != ['R']:
        return False
    filtered = receipt_image(proof['filter'])
    filtered_selection = proof.get('basis') == 'filtered_selectable_item'
    detail = None if filtered_selection else receipt_image(proof['detail'])
    inspected = receipt_image(proof['inspected_grid'])
    before = receipt_image(proof['before_selection'])
    selected = receipt_image(proof['selection'])
    return bool(vision.anchor(filtered, 'filter_r_only', .97)
                and vision.anchor(filtered, 'filter_worn_off', .97)
                and (filtered_selection or (vision.anchor(detail, 'detail_unlocked', .97)
                     and vision.anchor(detail, 'detail_wear_available', .97)))
                and any(c['point'] == card['point'] for c in visible_cards(inspected, vision, 'R'))
                and same_item(inspected, before, card)
                and selected_slot(vision, selected, card)
                and exactly_one_selected(vision, selected)
                and Path(selection).resolve() == Path(proof['selection']['path']).resolve())


def verify_visual_settlement(pending, result_path, grid_path):
    """Re-read bytes and anchors; missing evidence leaves the intent pending."""
    from equipment_vision import EquipmentVision
    vision = EquipmentVision()
    try:
        observation = pending['observation']
        if not verify_selection(vision, observation, pending['evidence']):
            return False
        confirmation = pending['confirmation']
        records = confirmation['images']
        if len(records) != 2 or records[0]['path'] == records[1]['path']:
            return False
        if not all(exactly_one_confirmed(vision, receipt_image(item)) for item in records):
            return False
        geometry = confirmation['geometry']
        point = [round(697 * geometry['width'] / 1280), round(467 * geometry['height'] / 720)]
        receipt = pending.get('input_receipt') or {}
        if receipt.get('input_kind') != 'left_click' or receipt.get('client_point') != point:
            return False
        result = pending['result_image']
        grid = pending['return_grid_image']
        if (Path(result_path).resolve() != Path(result['path']).resolve()
                or Path(grid_path).resolve() != Path(grid['path']).resolve()):
            return False
        return bool(vision.anchor(receipt_image(result), 'decompose_result_title', .97)
                    and vision.anchor(receipt_image(grid), 'grid_title', .97)
                    and vision.visible(receipt_image(grid), 'grid_title'))
    except (OSError, KeyError, TypeError, ValueError, cv2.error):
        return False
