"""Date-bound availability evidence, separate from completed mirror battles."""
from datetime import datetime, timezone


def verified_season_end(record, day):
    if not isinstance(record, dict):
        return False
    evidence = record.get('evidence') or {}
    return (record.get('state') == 'season_end' and record.get('cache_date') == day
            and record.get('auto_click_verified') is True
            and record.get('returned_to_main_menu') is True
            and isinstance(evidence, dict) and '赛季结算' in str(evidence.get('text', '')))


def settlement_from_report(report, day):
    """Accept the completed navigation branch only after an actual auto click."""
    if (not day or report.get('module') != 'mirror' or report.get('cache_date') != day
            or report.get('execute') is not True or report.get('completed') is not True
            or report.get('skipped') is not True or report.get('returned_to_main_menu') is not True
            or report.get('unavailable') != 'season_settlement'
            or report.get('error') or report.get('stopped') or report.get('pending')
            or (report.get('progress') or {}).get('pending')):
        return None
    observations = report.get('observations') or []
    last = observations[-1] if observations else {}
    observation = last.get('observation', last)
    actions = report.get('actions') or []
    if (observation.get('state') != 'mirror_settlement'
            or not any(a.get('type') == 'open_mirror_auto' and a.get('input_kind') == 'left_click'
                       for a in actions if isinstance(a, dict))):
        return None
    record = {'state': 'season_end', 'cache_date': day, 'auto_click_verified': True,
              'returned_to_main_menu': True, 'evidence': observation.get('settlement_evidence'),
              'observed_at': datetime.now(timezone.utc).isoformat()}
    return record if verified_season_end(record, day) else None
