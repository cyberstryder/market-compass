"""TradersPost webhook bridge for live futures automation.

Sends ICT detector signals to TradersPost as orders via webhook.
TradersPost routes to Tradovate (sim or live) with bracket orders.

Since 2026-10-18 the live book mirrors paper exactly: ONE position of
2 contracts at the detector's target per signal (the 2/2/1 scale-out
was removed, fb54017). There are no follow-up legs.

Enable with:
  TRADERSPOST_ENABLED=true
  TRADERSPOST_WEBHOOK_URL=https://webhooks.traderspost.io/trading/webhook/... (your TradersPost webhook URL, contains secret)

Only fires for detectors when ICT_FUTURES_PAPER_ENABLED is also on
(so paper and live stay in sync).

Safety:
- Micros only
- Bracket orders with per-leg stop/target from the detector
- Does NOT fire when paper is disabled
- Logs all webhook attempts via db.health for audit
- Webhook URL contains secret — never log it

Robustness (audit #4, 2026-10-21):
- Send-once: TradersPost documents NO idempotency/dedupe field (the
  Signal Message Reference ignores unknown fields; custom data belongs
  in "extras"). So dedupe is local: every send carries a deterministic
  signal key — 'entry:<trade id>' / 'close:<position id>' — which is
  claimed durably (traderspost_send:<key>) BEFORE the POST and never
  posted twice, even if the caller's transaction rolls back and the
  signal is submitted again. The key also rides in the payload's
  "extras" object for correlation on the TradersPost side.
- No network I/O inside the engine tick transaction: ict_paper.submit
  and engine.exits only QUEUE durable outbox rows
  (traderspost_outbox:<key>) in their transaction; Engine.tick calls
  dispatch_pending() after commit to perform the POSTs.
- Per-firm outcomes: each webhook's result is tracked and logged
  separately; a split outcome is audited as 'partial', never hidden
  behind an any() roll-up.
- HTTP 200 bodies are parsed: an error payload in a 200 body counts
  as a failure, not a send.
"""
import hashlib
import json
import time
import urllib.request
import urllib.error

# Map Compass micro symbols to TradersPost/Tradovate format
# Compass uses: MNQ.c.0, MES.c.0, MGC.v.0, etc.
# TradersPost expects: MNQ, MES, MGC, etc. (root symbol)
SYMBOL_MAP = {
    'MNQ.c.0': 'MNQ',
    'MES.c.0': 'MES',
    'MGC.v.0': 'MGC',
    'MCL.v.0': 'MCL',
    'MYM.c.0': 'MYM',
    'M2K.c.0': 'M2K',
    'M6A.c.0': 'M6A',
    'M6E.c.0': 'M6E',
    'MNQ': 'MNQ',
    'MES': 'MES',
    'MGC': 'MGC',
    'MCL': 'MCL',
    'MYM': 'MYM',
    'M2K': 'M2K',
    'M6A': 'M6A',
    'M6E': 'M6E',
}

# Per-webhook audit labels, in _webhook_urls() order. The URL itself is
# a secret and is never logged; the label is how a firm's outcome is
# named in audit records.
FIRM_LABELS = ('primary', 'secondary')

OUTBOX_PREFIX = 'traderspost_outbox:'
CLAIM_PREFIX = 'traderspost_send:'


def is_enabled(cfg):
    """Check if TradersPost bridge is enabled."""
    return bool(getattr(cfg, 'traderspost_enabled', False))


def _webhook_urls(cfg):
    """Return list of configured TradersPost webhook URLs.

    Supports a second webhook for a different prop firm (2026-10-18).
    Both get the same signals with the same rules.
    """
    urls = []
    url1 = getattr(cfg, 'traderspost_webhook_url', '')
    if url1:
        urls.append(url1)
    url2 = getattr(cfg, 'traderspost_webhook_url_2', '')
    if url2:
        urls.append(url2)
    return urls


def map_symbol(compass_symbol):
    """Map Compass symbol to TradersPost root symbol."""
    return SYMBOL_MAP.get(compass_symbol, compass_symbol)


# ---------------------------------------------------------------------------
# Signal identity (audit #4)
#
# Trade ids are deterministic — 'ict-<detector>-<symbol>-<signal_ts>-t1'
# (ict_paper.submit) — so the same signal re-submitted after a rollback
# reproduces the SAME id, and therefore the same signal key. That is
# what makes the send-once claim below an effective idempotency key.
# ---------------------------------------------------------------------------

def signal_key_for_trade(trade):
    """Deterministic idempotency key for an entry signal."""
    tid = trade.get('id')
    if tid:
        return 'entry:' + str(tid)
    basis = json.dumps(
        {k: trade.get(k) for k in
         ('detector', 'symbol', 'side', 'entry', 'stop', 'target', 'qty')},
        sort_keys=True, default=str)
    return 'entry:anon-' + hashlib.sha256(basis.encode()).hexdigest()[:24]


def close_key(ref):
    """Deterministic idempotency key for a close/flatten signal."""
    return 'close:' + str(ref)


def build_payload(cfg, trade):
    """Build TradersPost webhook JSON payload from a trade dict.

    trade needs: detector, symbol, side ('long'/'short'), entry, stop, target, qty
    For trailer legs (target=None), uses a far target (10R) so the position
    holds until stop or 15:45 flatten — matching the paper trailer behavior.
    Returns dict ready for JSON serialization, or None if invalid.
    """
    symbol = trade.get('symbol', '')
    ticker = map_symbol(symbol)

    side = trade.get('side', '').lower()
    # Fail closed on follow-up legs (audit #4): the dormant action='add'
    # branch is removed. Since the 2026-10-18 move to a single
    # 2-contract position (fb54017) no caller may send a second leg —
    # is_first_leg=False here means a caller bug, and guessing an 'add'
    # at the broker is how unplanned size happens. Refuse the payload.
    if not trade.get('is_first_leg', True):
        return None
    if side == 'long':
        action = 'buy'
    elif side == 'short':
        action = 'sell'
    else:
        return None

    entry = trade.get('entry')
    stop = trade.get('stop')
    target = trade.get('target')
    qty = trade.get('qty', 1)

    if not all([entry, stop]):
        return None
    if not qty or qty < 1:
        return None

    # Trailer leg: no fixed target (trailing stop in paper). Use far target
    # (10R) so TradersPost holds it until stop or 15:45 flatten.
    if target is None:
        risk = abs(entry - stop)
        if side == 'long':
            target = entry + risk * 10
        else:
            target = entry - risk * 10

    # Validate stop/target are on correct side
    if side == 'long':
        if not (stop < entry < target):
            return None
    else:  # short
        if not (target < entry < stop):
            return None

    # TradersPost payload format
    # See: https://traderspost.io/docs/webhooks and
    # https://blog.traderspost.io/article/correct-json-structure-for-traderspost-take-profit-webhooks
    # CRITICAL (2026-10-14): TradersPost expects nested objects, NOT flat fields.
    # - stopLoss: {"type": "stop", "stopPrice": X} (NOT "stop": X)
    # - takeProfit: {"limitPrice": X} (NOT "target": X)
    # - orderType: "market" (camelCase, NOT "order_type")
    # Flat "stop"/"target" fields are silently IGNORED — TradersPost places a
    # plain market order with NO bracket. That's why the 2026-10-14 Tradovate
    # SIM positions had no stops (MNQ -306, MES -52.50).
    payload = {
        'ticker': ticker,
        'action': action,
        'quantity': qty,  # Use leg qty (2 for the single live position)
        'price': entry,
        'stopLoss': {
            'type': 'stop',
            'stopPrice': stop,
        },
        'takeProfit': {
            'limitPrice': target,
        },
        # Metadata for TradersPost strategy tracking
        'strategy': trade.get('detector', 'compass'),
        'orderType': 'market',
        'timeInForce': 'day',
    }

    return payload


# ---------------------------------------------------------------------------
# Response inspection (audit #4)
# ---------------------------------------------------------------------------

_ERROR_STATUSES = {'error', 'failed', 'failure', 'rejected', 'invalid',
                   'denied'}


def _response_error(body):
    """Return the error text when an HTTP 200 body carries an error
    payload, else None.

    TradersPost can answer 200 for a signal it did not execute; the old
    code logged the body but never inspected it, so a rejected signal
    counted as sent. Only explicit error markers count as failure —
    an unparseable or marker-free body keeps the historical
    200-means-accepted reading.
    """
    try:
        data = json.loads(body)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    err = data.get('error') or data.get('errors')
    if err:
        return str(err)[:200]
    if data.get('success') is False:
        return str(data.get('message') or data.get('detail')
                   or 'success=false')[:200]
    status = str(data.get('status') or data.get('state') or '').strip().lower()
    if status in _ERROR_STATUSES:
        return 'status=%s' % status
    return None


# ---------------------------------------------------------------------------
# Audit + send-once claim (audit #4)
# ---------------------------------------------------------------------------

def _audit(db, status, detail, **extra):
    """Write the traderspost_webhook health/audit row. Never raises:
    audit must not break a send path (db.health opens its own tx)."""
    try:
        db.health('traderspost_webhook', status, detail, **extra)
    except Exception:
        pass


_CLAIM_PROBE = {'traderspost_claim_probe': True}


def _claim_send(db, key, record):
    """Durably claim a signal key BEFORE posting (send-once).

    Returns None when THIS caller claimed the key and must post;
    returns the prior claim dict when the signal was already attempted
    (caller must NOT post again); returns False when the claim could
    not be recorded (caller must NOT post — fail closed, because an
    unrecorded send cannot be deduped later).

    The claim is written in its own short transaction (never the
    caller's), so it survives a rollback of the engine tick that
    produced the signal — the exact path that used to double-fire:
    POST succeeds, tick rolls back, detector re-fires, second POST.
    locked_get serializes concurrent claimants on the key's row.
    """
    try:
        with db.tx() as c:
            prior = db.locked_get(c, CLAIM_PREFIX + key, dict(_CLAIM_PROBE))
            if isinstance(prior, dict) and not prior.get('traderspost_claim_probe'):
                return prior
            db.put(c, CLAIM_PREFIX + key, record)
        return None
    except Exception:
        return False


def _finish_claim(db, key, **fields):
    """Record the outcome on a claimed signal key. Never raises."""
    try:
        with db.tx() as c:
            rec = db.get(c, CLAIM_PREFIX + key)
            if isinstance(rec, dict):
                rec = dict(rec)
                rec.update(fields)
                db.put(c, CLAIM_PREFIX + key, rec)
    except Exception:
        pass


def _post_all(cfg, db, payload, signal_key):
    """POST payload to every configured webhook and audit per-firm.

    Returns (ok_any, summary, results). ok_any preserves the
    historical return contract — a fill at >=1 firm means the signal
    went live, which is what traderspost_sent and the fail-closed
    stop protection key off — but each firm's outcome is recorded
    separately and a split result is audited as 'partial', so one
    firm's failure is always visible (audit #4: the old `ok = any(...)`
    roll-up hid it entirely).
    """
    urls = _webhook_urls(cfg)
    data = json.dumps(payload).encode('utf-8')
    results = []
    for i, webhook_url in enumerate(urls):
        firm = FIRM_LABELS[i] if i < len(FIRM_LABELS) else 'webhook-%d' % (i + 1)
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode('utf-8', errors='ignore')[:500]
            err = _response_error(body)
            if err:
                results.append({'firm': firm, 'ok': False,
                                'detail': 'HTTP 200 error body: ' + err})
            else:
                results.append({'firm': firm, 'ok': True,
                                'detail': body[:120]})
        except urllib.error.HTTPError as e:
            err_body = e.read().decode('utf-8', errors='ignore')[:500]
            results.append({'firm': firm, 'ok': False,
                            'detail': 'HTTP %s: %s' % (e.code, err_body[:200])})
        except Exception as e:
            results.append({'firm': firm, 'ok': False,
                            'detail': 'send failed: %s' % str(e)[:200]})
    ok_any = any(r['ok'] for r in results)
    ok_all = all(r['ok'] for r in results)
    summary = '; '.join(
        '%s: %s' % (r['firm'], 'ok' if r['ok'] else 'FAILED - ' + r['detail'])
        for r in results)
    status = 'ok' if ok_all else ('partial' if ok_any else 'error')
    ticker = payload.get('ticker')
    action = payload.get('action')
    qty = payload.get('quantity')
    if qty is not None:
        # Audit #4: log the REAL quantity. This line used to hardcode
        # qty=1 while sends actually carried qty=2.
        detail = 'Sent %s %s qty=%s - %s' % (ticker, action, qty, summary)
    else:
        detail = '%s %s - %s' % (action, ticker, summary)
    _audit(db, status, detail,
           at=time.time(), ticker=ticker, action=action, qty=qty,
           signal_id=signal_key, partial=bool(ok_any and not ok_all),
           firms={r['firm']: r['ok'] for r in results})
    return ok_any, summary, results


def send_close_signal(cfg, db, symbol, ref=None):
    """Send a flatten/close signal to TradersPost for a symbol.

    Called at 15:45 CT session flatten (and by the fail-closed stop
    protection). Closes ALL open positions for that ticker in the
    TradersPost strategy. Returns (success, message).

    `ref` is the close's stable identity (the t1 position id). When
    given, the close is claimed send-once BEFORE posting; a repeat
    call with the same ref never posts twice. Without ref the close
    posts unclaimed (legacy callers).
    """
    if not is_enabled(cfg):
        return (False, 'TradersPost disabled')

    if not _webhook_urls(cfg):
        return (False, 'TRADERSPOST_WEBHOOK_URL not set')

    ticker = map_symbol(symbol)
    # CRITICAL (2026-10-14): TradersPost uses action "exit" to close positions,
    # NOT "close". "close" is silently ignored — positions stay open.
    payload = {
        'ticker': ticker,
        'action': 'exit',
        'strategy': 'compass-flatten',
        # Cancel resting bracket orders with the exit so no orphan stop or
        # target survives the flatten (TradersPost signal reference: the
        # cancel property combines order-cancelling with the action).
        # Note: 'strategy' is not a TradersPost routing field (ignored per
        # their docs) — scoping is by webhook URL, same as the entries.
        'cancel': True,
    }

    key = None
    if ref:
        key = close_key(ref)
        # Correlation id in the documented "extras" field (execution
        # ignores it; it is visible on the TradersPost signals page).
        payload['extras'] = {'signal_id': key}
        prior = _claim_send(db, key, {
            'kind': 'close', 'status': 'attempting', 'at': time.time(),
            'ticker': ticker, 'action': 'exit'})
        if prior is False:
            return (False, 'Send-once claim could not be recorded; not sending')
        if prior is not None:
            return (bool(prior.get('ok')),
                    'Duplicate suppressed: %s already attempted (%s)'
                    % (key, prior.get('detail', 'no detail recorded')))

    ok, summary, results = _post_all(cfg, db, payload, key)
    if key:
        _finish_claim(db, key,
                      status='sent' if ok else 'failed', ok=ok,
                      partial=bool(ok and not all(r['ok'] for r in results)),
                      firms={r['firm']: r['ok'] for r in results},
                      detail=summary[:300], responded_at=time.time())
    if ok:
        return (True, 'Flatten sent for %s: %s' % (ticker, summary))
    return (False, 'Flatten failed for %s: %s' % (ticker, summary))


def send_signal(cfg, db, trade):
    """Send a trade signal to TradersPost webhook.

    Returns (success, message). Logs to db.health for audit.

    Send-once (audit #4): the signal's deterministic key is claimed
    durably BEFORE posting. Re-sending the same trade identity —
    e.g. a tick rollback after a successful POST, followed by the
    detector re-firing the same signal — returns the recorded outcome
    instead of posting a second live order.
    """
    if not is_enabled(cfg):
        return (False, 'TradersPost disabled')

    if not _webhook_urls(cfg):
        return (False, 'TRADERSPOST_WEBHOOK_URL not set')

    payload = build_payload(cfg, trade)
    if not payload:
        return (False, 'Invalid trade (detector/side/prices)')

    key = signal_key_for_trade(trade)
    payload['extras'] = {'signal_id': key}
    prior = _claim_send(db, key, {
        'kind': 'entry', 'status': 'attempting', 'at': time.time(),
        'ticker': payload['ticker'], 'action': payload['action'],
        'qty': payload['quantity']})
    if prior is False:
        return (False, 'Send-once claim could not be recorded; not sending')
    if prior is not None:
        return (bool(prior.get('ok')),
                'Duplicate suppressed: %s already attempted (%s)'
                % (key, prior.get('detail', 'no detail recorded')))

    ok, summary, results = _post_all(cfg, db, payload, key)
    _finish_claim(db, key,
                  status='sent' if ok else 'failed', ok=ok,
                  partial=bool(ok and not all(r['ok'] for r in results)),
                  firms={r['firm']: r['ok'] for r in results},
                  detail=summary[:300], responded_at=time.time())
    if ok:
        return (True, 'Sent to TradersPost: ' + summary)
    return (False, 'TradersPost send failed: ' + summary)


# ---------------------------------------------------------------------------
# Outbox: queue inside the tick transaction, dispatch after commit
# (audit #4). The old code POSTed from inside ict_paper.submit /
# engine.exits — i.e. inside the engine tick's DB transaction, where a
# slow webhook held the transaction open (and Postgres is configured
# with idle_in_transaction / transaction timeouts that would kill the
# whole tick). Queueing is a plain state write in the caller's
# transaction, so it commits or rolls back atomically with the paper
# trade it belongs to.
# ---------------------------------------------------------------------------

def queue_signal(db, c, trade, trade_key=None, position_key=None):
    """Queue an entry send in the caller's transaction. NO network I/O.

    Returns the signal key, or None when the trade cannot produce a
    valid payload (caller marks the trade as failed, same as a send
    failure today). trade_key / position_key name the records
    dispatch_pending() writes the traderspost_sent outcome onto.
    """
    if not build_payload(None, trade):
        return None
    key = signal_key_for_trade(trade)
    db.put(c, OUTBOX_PREFIX + key, {
        'signal_id': key, 'kind': 'entry', 'status': 'queued',
        'queued_at': time.time(), 'trade': dict(trade),
        'trade_key': trade_key, 'position_key': position_key})
    return key


def queue_close_signal(db, c, symbol, ref=None):
    """Queue a close/flatten send in the caller's transaction.

    `ref` is the t1 position id — the close's send-once identity, so
    the same position's close can never be dispatched twice.
    """
    if ref:
        key = close_key(ref)
    else:
        key = 'close:adhoc-%s-%d' % (map_symbol(symbol),
                                     int(time.time() * 1000))
    db.put(c, OUTBOX_PREFIX + key, {
        'signal_id': key, 'kind': 'close', 'status': 'queued',
        'queued_at': time.time(), 'symbol': symbol, 'ref': ref})
    return key


def _apply_entry_outcome(db, row, ok, msg):
    """Write traderspost_sent (+ failure reason) onto BOTH the trade
    and position records. Both copies, per the 830c646 visibility fix:
    exits() closes a trade by copying the position record over the
    trade record, so a flag on only one copy is erased at close."""
    for key in (row.get('trade_key'), row.get('position_key')):
        if not key:
            continue
        try:
            with db.tx() as c:
                t = db.get(c, key)
                if t:
                    t = dict(t)
                    t['traderspost_sent'] = ok
                    if not ok:
                        t['traderspost_skip_reason'] = 'send_failed: ' + msg
                    db.put(c, key, t)
        except Exception:
            pass


def dispatch_pending(cfg, db, limit=50):
    """Dispatch queued TradersPost sends. Call OUTSIDE any DB
    transaction — Engine.tick calls this right after the tick
    transaction commits.

    Each row goes through send_signal / send_close_signal, which claim
    the signal send-once before posting. A row left 'queued' by a
    crash between POST and bookkeeping is therefore reconciled from
    its claim record on the next dispatch instead of re-posting.
    Returns the number of rows dispatched.
    """
    try:
        with db.tx() as c:
            rows = db.prefix(c, OUTBOX_PREFIX)
    except Exception:
        return 0
    pending = [r for r in rows.values()
               if isinstance(r, dict) and r.get('status') == 'queued']
    pending.sort(key=lambda r: r.get('queued_at') or 0)
    done = 0
    for row in pending[:limit]:
        ok, msg = False, ''
        try:
            if row.get('kind') == 'entry':
                ok, msg = send_signal(cfg, db, row.get('trade') or {})
                _apply_entry_outcome(db, row, ok, msg)
            else:
                ok, msg = send_close_signal(cfg, db, row.get('symbol', ''),
                                            ref=row.get('ref'))
        except Exception as e:
            ok, msg = False, 'dispatch error: %s: %s' % (
                type(e).__name__, str(e)[:200])
        try:
            with db.tx() as c:
                cur = db.get(c, OUTBOX_PREFIX + row['signal_id'])
                if isinstance(cur, dict):
                    cur = dict(cur)
                    cur.update(status='done', ok=bool(ok),
                               detail=str(msg)[:300],
                               dispatched_at=time.time())
                    db.put(c, OUTBOX_PREFIX + row['signal_id'], cur)
        except Exception:
            pass
        done += 1
    return done
