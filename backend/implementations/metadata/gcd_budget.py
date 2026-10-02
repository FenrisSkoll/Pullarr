"""Restart-safe rolling request accounting in the existing config store.

All attempts count across credential changes. No account identifier or secret is
stored. Anonymous requests obey both the hourly and daily ceiling; authenticated
requests obey the daily ceiling. An upstream 429 pauses both modes.
"""

import json
from datetime import timezone
from email.utils import parsedate_to_datetime
from math import isfinite
from time import time
from typing import Any, Callable, Optional

from backend.implementations.metadata.gcd_client import GcdError
from backend.internals.db import get_db


class GcdBudget:
    KEY = 'gcd_request_ledger_v1'

    def __init__(self, authenticated: bool, *, background: bool = False, clock: Callable[[], float] = time,
                 cursor: Callable[[], Any] = get_db):
        self.authenticated = authenticated
        self.background = background
        self.clock = clock
        self.cursor = cursor

    def _operate(self, required: int, charge: bool = False,
                 cooldown: Optional[float] = None) -> dict:
        cursor = self.cursor()
        if cursor.connection.in_transaction:
            raise GcdError('transaction_boundary')
        now = self.clock()
        cursor.execute('BEGIN IMMEDIATE')
        try:
            row = cursor.execute('SELECT value FROM config WHERE key=?', (self.KEY,)).fetchone()
            state = json.loads(row[0]) if row else {'attempts': [], 'until': 0}
            attempts, until = state['attempts'], state['until']
            background = state.get('background', [])
            if (not isinstance(attempts, list) or len(attempts) > 2000
                    or any(type(t) not in (int, float) or not isfinite(t) or t < 0 or t > now for t in attempts)
                    or type(until) not in (int, float) or not isfinite(until) or until < 0):
                raise ValueError('Invalid request ledger')
            attempts = sorted(t for t in attempts if t > now - 86400)
            if (not isinstance(background, list) or len(background) > 250
                    or any(type(t) not in (int, float) or not isfinite(t) or t < 0 or t > now for t in background)):
                raise ValueError('Invalid background ledger')
            background = sorted(t for t in background if t > now - 86400)
            hourly = [t for t in attempts if t > now - 3600]
            if cooldown is None:
                if until > now:
                    raise GcdError('rate_limited', until)
                if len(attempts) + required > 2000:
                    raise GcdError('deferred', attempts[0] + 86400 if attempts else now + 86400)
                if not self.authenticated and len(hourly) + required > 30:
                    raise GcdError('deferred', hourly[0] + 3600 if hourly else now + 3600)
                if self.background and len(background) + required > 250:
                    raise GcdError('deferred', background[0] + 86400 if background else now + 86400)
            if charge:
                attempts.append(now)
                if self.background:
                    background.append(now)
            state = {'attempts': attempts, 'background': background, 'until': max(until, cooldown or 0)}
            if charge or cooldown is not None:
                cursor.execute('''INSERT INTO config(key,value) VALUES(?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
                    (self.KEY, json.dumps(state, separators=(',', ':'))))
            cursor.connection.commit()
            return state
        except (ValueError, TypeError, KeyError):
            cursor.connection.rollback()
            raise GcdError('budget_invalid') from None
        except BaseException:
            cursor.connection.rollback()
            raise

    def preflight(self, requests: int) -> None:
        if type(requests) is not int or requests < 0:
            raise ValueError('Nonnegative request estimate required')
        if requests > (2000 if self.authenticated else 30):
            raise GcdError('snapshot_budget_unsupported')
        self._operate(requests)

    def charge(self) -> None:
        self._operate(1, charge=True)

    def limited(self, retry_after: Optional[str]) -> None:
        now = self.clock()
        # Missing/invalid server guidance conservatively uses the mode's window.
        until = now + (86400 if self.authenticated else 3600)
        if retry_after:
            try:
                seconds = int(retry_after)
                until = max(until, now + max(0, seconds))
            except ValueError:
                try:
                    stamp = parsedate_to_datetime(retry_after)
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    until = max(until, stamp.timestamp())
                except (ValueError, TypeError, OverflowError):
                    pass
        self._operate(0, cooldown=min(until, now + 7 * 86400))
