"""Append-only persistence for ICT futures scan outcomes (2026-10-26).

Fix 1 (+ scoped fix 3) from the AOI Fade fire-rate audit: production
realized only 3 of 39 replay signals, and 35 of 36 unmatched signals
could not be definitively attributed because declines were invisible —
aoi_fade / bos_gz_vwap overwrote a single health row with only the
latest decline (reason + symbol, details dropped), and every other
detector discarded ict_paper.submit()'s result entirely.

This module is MEASUREMENT ONLY. It never gates, alters, or retries a
submission; it only records what submit() already decided. Both entry
points are fault-isolated in the brake_shadow style: a failure here
must never raise into a scan or change a submission outcome, so every
body is wrapped and errors are swallowed silently (db.health() is NOT
used for the error path — it opens its own nested transaction, which
locks sqlite when a scan already runs inside one).

Decline events are deduplicated by the events-table key: the same
signal re-scanned (detectors re-submit a standing signal on every
scan) collapses to exactly one 'ict_decline' row per
(detector, symbol, signal, reason). Key basis: the signal's own
signal_ts when it carries one; otherwise a fingerprint of its
entry/stop/target levels plus the CT day (AOI Fade levels are a
deterministic function of the probe bar + zone).
"""
from datetime import datetime
from zoneinfo import ZoneInfo

CT = ZoneInfo('America/Chicago')

_SCALARS = (str, int, float, bool, type(None))


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _decline_key(detector, symbol, reason, sig, now):
    sig = sig or {}
    sts = _num(sig.get('signal_ts'))
    if sts:
        basis = '%d' % int(sts)
    else:
        day = datetime.fromtimestamp(now, CT).date().isoformat()
        basis = '%s:%.4f:%.4f:%.4f' % (
            day, _num(sig.get('entry')) or 0.0,
            _num(sig.get('stop')) or 0.0, _num(sig.get('target')) or 0.0)
    return 'ictdec:%s:%s:%s:%s' % (detector, symbol, basis, reason)


def record_decline(db, c, now, detector, symbol, res, sig=None):
    """Persist one submit() decline as an 'ict_decline' event.

    `res` is the dict ict_paper.submit() returned. No-op (returns
    False) when the submission succeeded or res is malformed. Returns
    True only when a NEW event row was inserted (repeat declines of
    the same signal+reason return False via the dedupe key). Never
    raises.
    """
    try:
        if not res or res.get('submitted'):
            return False
        reason = res.get('reason') or 'unknown'
        sig = sig or {}
        details = {k: v for k, v in res.items()
                   if k not in ('submitted', 'reason')
                   and isinstance(v, _SCALARS)}
        payload = {
            'detector': detector,
            'symbol': symbol,
            'reason': reason,
            'direction': sig.get('direction') or sig.get('side'),
            'entry': _num(sig.get('entry')),
            'stop': _num(sig.get('stop')),
            'target': _num(sig.get('target')),
            'signal_ts': _num(sig.get('signal_ts')),
            'level_kind': sig.get('level_kind'),
            'details': details,
        }
        return bool(db.append(c, 'ict_decline', 'ict_futures', symbol, now,
                              payload,
                              key=_decline_key(detector, symbol, reason,
                                               sig, now)))
    except Exception:
        return False


def record_scan_eval(db, c, now, detector, symbol, zones=None, signals=0,
                     submitted=0, declined=0, extra=None):
    """Persist one scan-evaluation summary as an 'ict_scan_eval' event.

    Scoped (audit fix 3) to detectors whose scans do not already log
    per-symbol evaluations: aoi_fade and turtle_soup call this only
    when the scan actually evaluated at least one signal candidate
    for the symbol, keeping the event stream sparse. Combined with
    'ict_decline' (why a candidate was refused) and the per-detector
    signal events (what was accepted), this separates "never
    evaluated" from "evaluated and declined" permanently. Never
    raises.
    """
    try:
        payload = {
            'detector': detector,
            'symbol': symbol,
            'zones': zones,
            'signals': int(signals or 0),
            'submitted': int(submitted or 0),
            'declined': int(declined or 0),
        }
        if extra:
            payload.update({k: v for k, v in extra.items()
                            if isinstance(v, _SCALARS)})
        return bool(db.append(c, 'ict_scan_eval', 'ict_futures', symbol,
                              now, payload,
                              key='icteval:%s:%s:%d' % (detector, symbol,
                                                        int(now))))
    except Exception:
        return False
