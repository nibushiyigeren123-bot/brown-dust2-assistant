"""活动通用AP只读取固定右上数字，不依赖活动图标或体力名称。"""
import re

AP_ROI = (1115, 10, 1278, 58)


def resource_balance(doc):
    """[资源] 完整且唯一的X/5才成立；未知、其他分母和歧义不当作零。"""
    rows = doc.within(AP_ROI)
    matches = []
    # [边界] 同一区域任何不完整或冲突的分数字段均拒绝，防止截取X/50中的X/5。
    fractions = [row for row in rows if '/' in row['text']]
    if len(fractions) != 1:
        return {'free_ap': None, 'free_ap_capacity': None, 'owned_extra_ap': None}
    row = fractions[0]
    match = re.fullmatch(r'([0-5])/5(?:[|丨]?\+(\d[\d,]*))?', row['text'])
    if not match:
        return {'free_ap': None, 'free_ap_capacity': None, 'owned_extra_ap': None}
    extra = int(match[2].replace(',', '')) if match[2] else None
    # [额外AP] 仅接纳同行右侧完整+数字；没有读数始终保留None。
    right = [r for r in rows if r['box'][0] >= row['box'][2] - 3
             and abs(r['point'][1] - row['point'][1]) < 15
             and re.fullmatch(r'\+\d[\d,]*', r['text'])]
    if extra is None and len(right) == 1:
        extra = int(right[0]['text'][1:].replace(',', ''))
    elif len(right) > 1 or (extra is not None and right):
        return {'free_ap': None, 'free_ap_capacity': None, 'owned_extra_ap': None}
    return {'free_ap': int(match[1]), 'free_ap_capacity': 5, 'owned_extra_ap': extra,
            'resource_text': row['text'], 'resource_source': 'fixed_header_X_over_5_OCR',
            'resource_family': 'free_event_ap', 'resource_policy_supported': True,
            'resource_semantics_evidence': 'fixed_event_AP_ROI_complete_X/5'}
