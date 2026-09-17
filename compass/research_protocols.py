"""Dated review definitions. Metadata only; never selects trades or rewrites evidence."""
from copy import deepcopy

VERSION = 'research-protocols-2026-09-17-v1'
DOC = 'https://github.com/cyberstryder/market-compass/blob/main/docs/research-protocols.md'
EVALUATION = ('First 10 complete exchange sessions beginning on or after 2026-09-18 '
              'and after this protocol is deployed. Lock membership by entry session; '
              'wait for all required endpoints. Earlier observations are development evidence. '
              'Coverage diagnostics may be inspected; do not retune using evaluation outcomes.')


def definition(model, population, entry, contract, exit, costs, primary, exclusions,
               document, evaluation=EVALUATION, status='frozen_review_definition'):
    return dict(registry_version=VERSION, frozen_on='2026-09-17', status=status,
        model=model, population=population, entry=entry, contract=contract, exit=exit,
        costs=costs, primary=primary, exclusions=exclusions, evaluation=evaluation,
        href=DOC, implementation_document=document,
        uncertainty='Report eligible, measured and missing counts by session and symbol; '
                    'overlapping records and shared contracts are correlated. Report a '
                    '95% session-cluster bootstrap interval for a prespecified difference '
                    'only with at least ten distinct entry sessions with usable outcomes; otherwise inconclusive. '
                    'Intervals are a review requirement, not an existing dashboard calculation.')


PROTOCOLS = {
    'morning': definition('morning_stock_report_v1 / saved Pine and configuration digest',
        'Retained non-test baseline signals and separate research candidates; source and native tapes never pooled.',
        'Saved signal candle close; 1-minute native bars with original source/settings identity.',
        'Underlying only; no option contract or expiry in stock comparisons.',
        '5/15/30/60-minute closes and +1/-0.5%, +2/-1% brackets with 60-minute timeout; 120/180-minute research endpoints secondary.',
        'Gross underlying percentage moves; no executable fill, fees or slippage model.',
        '60-minute close versus +1/-0.5% bracket on the same complete, unambiguous 60-candle cohort; other rules exploratory.',
        'Missing/conflicting candles, changed settings and same-candle ambiguous bracket touches excluded and counted.',
        'docs/morning-stock-reports.md'),
    'morning-expirations': definition('atm_expiration_comparison_v1 / ask_bid_fees_v1',
        'Fresh non-test Morning stock alerts with common complete entry and 5/15/30/60-minute quotes.',
        'Observed ask after signal within original 120-second window; paired quote clocks within five seconds.',
        'Standard CALL; nearest strike, lower strike tie; baseline earliest expiry after today, 0DTE actual same-day listing, 1–3 DTE aliases baseline when eligible.',
        'Fixed observed bids at 5/15/30/60 minutes; best observed exit is hindsight only.',
        '$0.65/side/contract; stress adds $0.01/share/side; no equal-risk claim.',
        'Paired 60-minute net premium-return difference: 0DTE minus baseline; other horizons secondary.',
        'Missing listings, stale/pre-signal quotes, incomplete catalogs, timing skew and missing common endpoints.',
        'docs/morning-expiration-comparison.md'),
    'smoothers': definition('native-smoothers-v1 / frozen weekly configuration revision',
        'All enabled saved configurations (76 at audit), including unfeatured observations; match source by week and ticker.',
        'Original weekly model reference at 10:30 ET on first exchange session; retain actual generation and receipt clocks.',
        'Latest listed expiry from Wednesday through Friday of the signal week, nearest strike with lower-strike tie; source/native contracts compared explicitly.',
        'First observed stock target touch or original week close; premium valuations and underlying outcomes separate.',
        'Stock target states and source/native valuation parity; no modeled option value presented as an executed net return.',
        'Native/source direction, entry, target, quality tier and terminal-state agreement on matched week/ticker/configuration.',
        'Ambiguous identities, missing source/native week, missing premium quotes and unresolved stock paths stay separate.',
        'docs/smoothers-sender-handoff.md',
        evaluation='First two complete native weeks starting on or after 2026-09-21 after deployment; wait for both weekly closes. September 14 is migration/development evidence. Delivery and backup gates remain independent.'),
    'intraday': definition('setup-outcomes-v3 / sampled-bracket-v2 / compass-scanner-v2',
        'All admitted stock/ETF setup trials including cooldown candidates; group by rule, side, symbol and version.',
        'Fresh executable post-signal quote; one unit and one adverse tick, frozen invalidation.',
        'Underlying shares; no option expiry or strike.',
        'Outward-rounded stop, 2R capped target, or 15 minutes before cash close.',
        'Bid/ask plus one adverse entry/market-exit tick; stock fee placeholder zero.',
        'Net R per resolved trial by prespecified rule; report alerted and cooldown cohorts separately, with missingness.',
        'Invalid entry, quote gap over 15 seconds or missing deadline quote; overlapping experiments are not portfolio returns.',
        'docs/setup-outcomes.md'),
    'futures': definition('setup-outcomes-v3 / sampled-bracket-v2 / futures-market-assessment-v1 / futures-entry-variants-v1',
        'All admitted dated-contract futures trials, including cooldown and research-only late-session candidates; instruments remain separate.',
        'Fresh post-signal executable quote, one contract, frozen structural stop; retain no-candidate bar census separately.',
        'Resolved dated instrument and multiplier; never substitute continuous chart prices.',
        '2R target or observed stop; v3 research trials exit one minute before calendar close. Portfolio/account cutoff is separate.',
        'One adverse tick on entry and market/stop exit plus saved instrument fee ($1.50/side for micros, $2.50/side for standard contracts).',
        'Primary entry contrast: first_per_trend versus repeated, using same trial outcomes and net R per eligible opportunity (skipped=zero exposure). Market-state selection and pullback_reset are secondary.',
        'Unknown regime, missing/stale bars, unmapped contract, excluded entry or quote gaps; no-candidate bars are not losing trials.',
        'docs/futures-entry-variants.md'),
    'option_ideas': definition('option-ideas-v2 / paired-recorded-quotes-v2 / quote-continuity-v1',
        'All retained intraday option idea candidates from configured stock scanner; independent one-contract observations.',
        'Qualifying fresh stock/option quotes within two minutes of trigger and before close minus 30 minutes.',
        'Standard CALL/PUT; 1–21 DTE prefer 7, strike within 3%, OI/day volume >=100, supplied delta magnitude 0.30–0.70; saved missing-delta fallback.',
        '30% premium stop, 50% capped target, original underlying bracket, or close minus 15 minutes.',
        'Ask+$0.01 to bid-$0.01, $0.65/side; overlapping positions are not an account.',
        'Net premium return by frozen setup/direction on resolved v2 paths; report exclusion and unresolved rates beside returns.',
        'Unavailable contract/stream slot, failed liquidity/continuity, trigger invalidation and >15-second live gaps.',
        'docs/options-ideas.md'),
    'swing_ideas': definition('swing-ideas-v1',
        'Admitted swing option candidates from saved technical/flow rules; rejected technical candidates belong to separate flow study.',
        'Fresh stock/option quotes within original two-minute admission window; cash session until close minus 30 minutes.',
        'Standard CALL/PUT; 14–60 DTE prefer 30, nearest qualifying strike within 3%, recorded liquidity/delta rules.',
        '35% premium stop, 75% capped target, underlying bracket, or 10 sessions including entry / session before expiry, whichever earlier.',
        'Ask+$0.01 to bid-$0.01, $0.65/side; adverse overnight gaps retained.',
        'Net premium return by frozen setup/direction; compare coverage and gap incidence, not underlying-study returns.',
        'Missing open-market path beyond 60-second budget, metadata/reference changes or missing deadline quotes unresolved.',
        'docs/swing-ideas.md'),
    'secondary': definition('secondary-context-v4',
        'Received Morning, Smoothers, TradingView futures, native futures and Obsidian candidates; source families separate.',
        'Same timely review-time underlying midpoint for every selection arm; original 60-second availability window.',
        'Underlying direction only; original option identity retained as context.',
        '15/30/60-minute checkpoints; no carry across cash close/futures research deadline; weekly source states separate.',
        'Underlying movement before costs; neither fills nor option profit.',
        '60-minute directional move per eligible candidate: supported filter (unselected=zero exposure) versus all; TM/level removal contrast secondary.',
        'Late input, insufficient required context, missed quote window, session boundary; same reference and endpoint for supported/rejected.',
        'docs/secondary-review.md'),
    'obsidian': definition('watchlist_ideas_v1 / retained unversioned option observations',
        'Retained provider ideas and revisions; exclude configured DJT from active tracking; update messages do not mean failure.',
        'First fresh option ask after timely receipt; may wait for open; preserve anchor delay and late imports.',
        'Exact source-listed OCC contract; no changed strike/expiry or synthetic replacement.',
        'Current observed bid and sampled extrema through expiry; no fixed exit/terminal win rule implemented.',
        'Ask-to-bid liquidation percentage before fees/slippage; vendor reported percentages separate.',
        'Observation coverage and source-update association only. No win-rate or trading-edge claim until a separately versioned exit protocol exists.',
        'Late imports, unanchored paths, >15-second gaps, ambiguous revisions, historical reconstructed evidence separate.',
        'docs/obsidian-watchlist.md', status='frozen_observation_definition'),
    'zero-dte': definition('0dte-selection-v1 / saved ORB or scanner strategy / sampled-bracket-v2',
        'Same-day portfolio option candidates and admitted positions; shared loss, risk, position and entry limits retained.',
        'Fresh post-trigger bid/ask within original pending deadline and available portfolio capacity.',
        'Listed same-day standard CALL/PUT chosen by existing engine; actual contract metadata and selection reasons retained.',
        'Stop distance max($0.05, 30% of observed ask), 2R target or close minus 15 minutes; original portfolio management model retained.',
        'Saved bid/ask adverse tick model and $0.65/side/contract.',
        'Selection coverage and cost-adjusted closed-position results; no attribution of a historical rejection to current risk state.',
        'Expired retries, missing diagnostic history, quote/liquidity rejection, portfolio capacity and unresolved management.',
        'docs/research-admin.md'),
    'vendor-research': definition('source-specific context versions and clocks',
        'Configured vendor screeners/matrices, local exposure and received responses; not the full market.',
        'No entry; preserve source timestamp, receipt timestamp, cached/stale flags and missing cells.',
        'N/A: context inventory, not contract selection.', 'No standalone return endpoint.',
        'N/A: no fill or P&L model.',
        'Freshness, availability and linked setup/secondary coverage; no standalone screener success rate.',
        'Missing clocks, stale values, partial pagination and unverified vendor methodology remain explicit.',
        'docs/vendor-freshness-and-review-periods.md', status='frozen_context_definition'),
}

# Existing prospective protocols are linked without changing their original cohorts.
for key, model, document, primary in (
    ('tm-flow', 'tm-receipt-study-v1', 'docs/tm-scoring-study.md', '60 trading-minute common-score directional underlying comparison.'),
    ('swing-flow-study', 'swing-flow-study-v1', 'docs/swing-flow-study.md', 'Five later session closes on common candidate-time price references.'),
    ('spy', 'spy-plan-0dte-v1', 'docs/spy-plan-outcomes.md', 'Delivered confirmation to same-day one-contract net outcome; one observation/session.')):
    PROTOCOLS[key] = dict(registry_version=VERSION, model=model, status='existing_frozen_protocol',
        frozen_on='2026-09-16', href='https://github.com/cyberstryder/market-compass/blob/main/'+document,
        implementation_document=document, primary=primary,
        evaluation='Retain the original dated protocol and activation cohort; this registry does not restart or reclassify it.')


def protocol(key):
    return deepcopy(PROTOCOLS[key])
