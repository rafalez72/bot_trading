"""Parser del timestamp de expiry desde el slug del market.

Extraído de executor.py el 2026-05-06 para que paper.py también pueda usar
el filtro `expires_too_soon` sin importar de executor (que importaría circular
porque executor importa de paper).
"""
from __future__ import annotations

import re

_MONTH_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def parse_slug_expiry(slug: str | None) -> int | None:
    """Extrae el timestamp UTC de expiry del slug usando múltiples estrategias.

    Estrategias en orden:
      1. Epoch unix al final.
      2. <month>-<day>-<year>-<H><am|pm>(-et)? → fecha + hora ET (UTC-4 EDT).
      3. -YYYY-MM-DD$ al final → fecha cruda. END of day (23:59:59 UTC).

    Devuelve epoch UTC o None si no parseó.
    """
    if not slug:
        return None
    s = slug.lower()

    # 1. Epoch al final
    m = re.search(r"-(\d{10,13})$", s)
    if m:
        try:
            ts = int(m.group(1))
        except (TypeError, ValueError):
            ts = None
        if ts is not None:
            if ts > 10**12:
                ts = ts // 1000
            if 1700000000 < ts < 1900000000:
                return ts

    # 2. <month>-<day>-<year>-<H><am|pm>(-et)?
    months_pat = "|".join(_MONTH_MAP.keys())
    m = re.search(
        rf"-({months_pat})-(\d{{1,2}})-(\d{{4}})-(\d{{1,2}})(am|pm)(?:-et)?$",
        s,
    )
    if m:
        try:
            from datetime import datetime, timezone, timedelta
            month = _MONTH_MAP[m.group(1)]
            day = int(m.group(2))
            year = int(m.group(3))
            hour = int(m.group(4))
            if m.group(5) == "pm" and hour != 12:
                hour += 12
            if m.group(5) == "am" and hour == 12:
                hour = 0
            utc_dt = datetime(year, month, day, hour, 0, tzinfo=timezone.utc) + timedelta(hours=4)
            return int(utc_dt.timestamp())
        except Exception:
            pass

    # 3. -YYYY-MM-DD$
    m = re.search(r"-(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        try:
            from datetime import datetime, timezone
            year = int(m.group(1))
            month = int(m.group(2))
            day = int(m.group(3))
            event_utc = datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)
            return int(event_utc.timestamp())
        except Exception:
            pass

    return None
