"""Interpret already-recognized resource tokens; this module does not perform OCR."""
import re
import unicodedata


def parse_resource_token(text):
    token = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))
    match = re.fullmatch(r'(\d+)/(\d+)(?:\|?\+(\d+))?', token)
    if not match or int(match[2]) <= 0:
        return None
    return {'free': int(match[1]), 'free_capacity': int(match[2]),
            'owned_extra': int(match[3]) if match[3] is not None else None}


def available_for_policy(free, owned_extra, free_only, required):
    if type(free) is not int or free < 0 or type(required) is not int or required <= 0:
        return None
    if free_only:
        return free
    if type(owned_extra) is int and owned_extra >= 0:
        return free + owned_extra
    # Preserve the supported case where known free resources cover this action.
    # An unknown extra pool cannot fill a free-resource deficit.
    return free if free >= required else None
