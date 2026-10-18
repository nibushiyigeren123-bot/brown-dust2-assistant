"""BrownDust II game day: 08:00 Beijing time, independent of host timezone."""
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))

def game_day(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Game-day timestamps require an explicit timezone')
    return (now.astimezone(BEIJING) - timedelta(hours=8)).date()

def check_execution_day(expected):
    if expected != game_day().isoformat():
        raise RuntimeError('北京时间08:00游戏日已切换；本轮已停步，保留原日期回执和未决记录，请重新核对。')
