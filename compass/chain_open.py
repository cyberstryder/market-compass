"""Morning option-chain snapshot archive (2026-11-03, OMC substrate).

The morning twin of ``chain_eod``: ONE snapshot per underlying per
trading day, taken just after the open, retained forever under the
distinct event kind ``chain_open``. It exists because the Overnight
Morning-Change registration (files/overnight-morning-change-
registration-2026-11-03.md, frozen, sha256 23a1f84a...) prices its
exits into the next morning's first trustworthy post-open quotes —
Josh, 2026-11-03: "there are times I need to sell before 9:45 to
get the move." The morning move is front-loaded, so the snapshot
trigger fires at 09:35 ET and, on the measured EOD pull duration
(37.8 s on 2026-11-03, 54.6 s on 2026-11-02, same provider path and
concurrency), completes by ~09:36-09:37 ET.

Everything else mirrors ``chain_eod`` exactly — same provider path
(Massive primary, Alpaca fallback, ``full_greeks=True``), same
filter (+/-10% of the underlying reference, expiries <= 60 DTE),
same contract fields, same per-day manifest with ok/error
accounting, same health surfacing (under kind ``chain_open``), same
credential preflight. The machinery is shared: this module only
pins the kind. Storage: events kind ``chain_open``, key
``chain_open:<date>:<SYM>``; manifest state ``chain_open:<date>``;
registered in ``copy_archive.RETENTION`` and
``archive_prune.EVENT_POLICY`` at the same permanence horizon as
``chain_eod`` — the archive is the point.

Trigger: POST/GET ``/api/admin/chain-open-snapshot`` (cron-token
gated, see app.py), fired weekdays at 08:35 CT (09:35 ET) by the
goal cron ``morning-chain-snapshot``. History before the first
fire does not exist and is never fabricated.
"""
from . import chain_eod

KIND = 'chain_open'


def read_manifest(db, date_str):
    return chain_eod.read_manifest(db, date_str, kind=KIND)


async def run_snapshot(db, cfg, **kw):
    kw['kind'] = KIND
    return await chain_eod.run_snapshot(db, cfg, **kw)


def start_snapshot_background(db, cfg, **kw):
    kw['kind'] = KIND
    return chain_eod.start_snapshot_background(db, cfg, **kw)
