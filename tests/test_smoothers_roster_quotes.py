"""Roster rows show the publication-batch refreshed quote (bid/ask + timestamp),
never a stale modeled premium. Regression for 2026-10-05: Josh traded GS off
the roster's 'Option cost $1,247.50' (modeled at the 09:30 freeze) and got
filled at $13.70 — the number had no timestamp and no spread."""
from compass import smoothers_messages as messages


def _row(**kw):
    base=dict(ticker='GS',direction='CALL',week='2026-10-05',id='GS',
              entry_price=893.15,target_price=902.09,
              contract=dict(symbol='GS261009C00892500',strike=892.5,expiration='2026-10-09'),
              quality_tier='A+',featured_rank=10,quality_score=84.42,target_atr_mult=0.374,
              config=dict(backtest_wr=0.85),stats_at_entry=dict(wins=5,losses=0))
    base.update(kw)
    return base


def _content(rows,now=1791210973):
    return '\n'.join(p['content'] for p in messages.roster(rows,'2026-10-05',now))


def test_roster_row_shows_refreshed_bid_ask_and_timestamp():
    p=_row(quote=dict(status='available',bid=12.40,ask=12.55,midpoint=12.475,
                      quote_at_ms=1791210973000),
           entry_premium=12.475,est_return_pct=41.0)
    text=_content([p])
    assert 'Option $12.40/$12.55 (mid $12.47) @ 09:36 CT' in text
    assert 'Est return +41%' in text
    assert 'Option cost' not in text


def test_roster_row_hides_stale_modeled_premium_when_quote_unavailable():
    # Stale modeled premium from the per-ticker processing pass must never
    # render as a price once the batch quote is unavailable.
    p=_row(quote=dict(status='unavailable',reason='missing_batch_quote'),
           entry_premium=5.925,est_return_pct=56.0)
    text=_content([p])
    assert 'Option quote unavailable' in text
    assert '$592.50' not in text and '5.925' not in text
    # A return computed off a price that is not displayed must not display.
    assert 'Est return Unavailable' in text
    assert '+56%' not in text


def test_roster_row_unavailable_without_any_quote():
    p=_row(quote=None,entry_premium=12.475,est_return_pct=41.0)
    text=_content([p])
    assert 'Option quote unavailable' in text
    assert 'Est return Unavailable' in text


def test_roster_row_unavailable_on_wide_spread_without_numbers():
    p=_row(quote=dict(status='wide_spread',bid=None,ask=None,midpoint=None,quote_at_ms=1791210973000),
           entry_premium=None,est_return_pct=None)
    text=_content([p])
    assert 'Option quote unavailable' in text
