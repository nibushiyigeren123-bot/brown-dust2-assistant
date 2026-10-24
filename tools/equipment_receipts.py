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
    # [兼容] 新路线独立验证，旧批量选择basis仍保留原证据合同。
    if proof.get('basis')=='detail_single_item':return verify_detail_selection(vision,observation,selection)
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


def verify_detail_filter(vision,proof):
    """[筛选回执] 单品质应用前真实页与应用后前景普通列表分别核对。"""
    try:
        qualities=proof['qualities']
        if proof.get('basis')!='detail_filter_applied' or qualities not in (['R'],['N']):return False
        before=receipt_image(proof['before']);grid=receipt_image(proof['grid'])
        return bool(vision.anchor(before,'filter_title') and vision.anchor(before,'filter_'+qualities[0].lower()+'_only',.97)
                    and vision.anchor(grid,'grid_title',.97) and vision.visible(grid,'grid_title'))
    except (OSError,KeyError,TypeError,ValueError,cv2.error):return False


def verify_detail_selection(vision,observation,confirmation_path):
    """[详情单件链] 原始字节重新识别品质、锁定、穿戴、同格输入和确认1件；库存不提供捷径。"""
    from equipment_workflow import visible_cards
    from daily_vision import OcrDocument,windows_ocr
    try:
        proof=observation['visual_receipt'];card=proof['card'];quality=card['quality']
        if quality not in ('R','N') or observation['selected_qualities']!=[quality]:return False
        if not verify_detail_filter(vision,proof['filter']):return False
        if proof['filter']['qualities']!=[quality]:return False
        if quality=='N':
            # [N后备] 新basis同样要求R筛选下两张明确零数量和空列表，不能只信任r_unavailable布尔值。
            from equipment_vision import read_inventory_count
            absent=proof.get('r_unavailable_evidence') or {}
            empty=absent.get('images') or []
            if (observation.get('r_unavailable') is not True or absent.get('filtered_count')!=0
                    or len(empty)!=2 or empty[0]['path']==empty[1]['path']
                    or absent.get('filter',{}).get('qualities')!=['R']
                    or not verify_detail_filter(vision,absent['filter'])):return False
            for item in empty:
                frame=receipt_image(item)
                if not vision.anchor(frame,'grid_empty',.97):return False
                if read_inventory_count(frame,Path(item['path']).parent/'n_absence_ocr').get('value')!=0:return False
        grid=receipt_image(proof['inspected_grid']);input_grid=receipt_image(proof['input_grid'])
        if not any(c['point']==card['point'] for c in visible_cards(grid,vision,quality)):return False
        if not same_item(grid,input_grid,card):return False
        geometry=proof['geometry'];opened=proof['open_detail_input']
        point=[round(card['point'][0]*geometry['width']/1280),round(card['point'][1]*geometry['height']/720)]
        if opened.get('input_kind')!='left_click' or opened.get('client_point')!=point:return False
        scissors=proof['open_confirmation_input']
        scissors_point=[round(349*geometry['width']/1280),round(490*geometry['height']/720)]
        if scissors.get('input_kind')!='left_click' or scissors.get('client_point')!=scissors_point:return False
        details=proof['details'];confirmations=proof['confirmations']
        if len(details)!=2 or len(confirmations)!=2:return False
        if details[0]['path']==details[1]['path'] or confirmations[0]['path']==confirmations[1]['path']:return False
        if Path(confirmation_path).resolve()!=Path(confirmations[-1]['path']).resolve():return False
        flags=[]
        for item in [*details,proof['input_detail']]:
            frame=receipt_image(item);output=Path(item['path']).parent/'single_receipt_ocr'
            doc=OcrDocument(windows_ocr(Path(item['path']),output/'page'))
            flags.append(vision.detail_single_flags(frame,doc,output))
        if any(f.get('quality')!=quality or f.get('unlocked') is not True or f.get('not_equipped') is not True
               or f.get('scissors_verified') is not True or f.get('detail_close_verified') is not True for f in flags):return False
        if not flags[0]['item_title'] or any(f['item_title']!=flags[0]['item_title'] for f in flags):return False
        return all(exactly_one_confirmed(vision,receipt_image(item)) for item in confirmations)
    except (OSError,KeyError,TypeError,ValueError,cv2.error):return False


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
        if (pending['observation'].get('visual_receipt',{}).get('basis')=='detail_single_item'
                and records!=pending['observation']['visual_receipt']['confirmations']):return False
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
