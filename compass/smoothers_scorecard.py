"""Keep stock target outcomes separate from executable-quote option estimates."""
from collections import Counter
from .market import number

FEE_PER_CONTRACT = 1.32


def option_result(p, fee=FEE_PER_CONTRACT):
    if p.get('status') not in ('WIN', 'LOSS'):
        return dict(status='pending', reason='underlying_signal_unresolved', net_pnl=None)
    contract = p.get('contract') or {}
    quotes = [p.get('quote') or {}, p.get('exit_quote') or {}]
    for label, q in zip(('entry', 'exit'), quotes):
        bid, ask = number(q.get('bid')), number(q.get('ask'))
        age = number(q.get('quote_age_seconds'))
        if (q.get('status') != 'available' or bid is None or ask is None or
                not 0 <= bid <= ask or ask <= 0 or age is None or not 0 <= age <= 5):
            return dict(status='unavailable', reason=label+'_quote_not_usable', net_pnl=None)
        if not contract.get('symbol') or (q.get('contract') or {}).get('symbol') != contract['symbol']:
            return dict(status='unavailable', reason=label+'_contract_mismatch', net_pnl=None)
    start, end = (number(q.get('quote_at_ms')) for q in quotes)
    resolution = number(p.get('resolution_time'))
    if start is None or end is None or end < start or resolution is None or end < resolution*1000-5000:
        return dict(status='unavailable', reason='exit_quote_time_not_valid', net_pnl=None)
    multiplier = number(contract.get('multiplier'))
    if multiplier != 100:
        return dict(status='unavailable', reason='unsupported_contract_multiplier', net_pnl=None)
    net = round((quotes[1]['bid']-quotes[0]['ask'])*multiplier-fee, 2)
    return dict(status='profit' if net > 0 else 'loss' if net < 0 else 'breakeven',
                net_pnl=net, fee=fee, quantity=1, entry_ask=quotes[0]['ask'], exit_bid=quotes[1]['bid'])


def summarize(rows, fee=FEE_PER_CONTRACT):
    states = Counter(); options = Counter(); reasons = Counter(); details = []; net = 0.; measured = 0; false_wins = 0; measured_hits = 0
    for p in rows:
        states[p.get('status', 'UNKNOWN')] += 1
        result = option_result(p, fee)
        options[result['status']] += 1
        if result.get('reason'): reasons[result['reason']] += 1
        if result['net_pnl'] is not None:
            measured += 1; net += result['net_pnl']
            measured_hits += p.get('status') == 'WIN'
            false_wins += p.get('status') == 'WIN' and result['net_pnl'] < 0
        details.append(dict(ticker=p.get('ticker'), underlying_status=p.get('status'), **result))
    return dict(underlying_states=dict(states), option_states=dict(options), measured=measured,
        total=len(details), net_pnl=round(net, 2) if measured else None,
        target_hits_measured=measured_hits,target_hits_with_option_loss=false_wins, missing_reasons=dict(reasons), details=details,
        fee_per_contract=fee,
        basis='One-contract entry ask to target/close observation bid, less stated fee assumption. '
              'Available quotes only; not broker fills or a complete portfolio return. Pending and missing quotes are excluded.')
