"""Read-only gap annotations; never substitute candles or alter outcome coverage."""
import json
from pathlib import Path

EVIDENCE = json.loads(Path(__file__).with_name('chart_gap_evidence.json').read_text())


def chart_gaps(signal, coverage):
    missing = set(coverage['unreceived_elapsed_minutes'])
    evidence = next((r for r in EVIDENCE['records'] if all(signal[k] == r[k]
        for k in ('ticker', 'signal_at_ms', 'script_version'))), None)
    known = set(evidence['minutes']) if evidence else set()
    return {'chart_absent_minutes': sorted(missing & known),
        'unclassified_minutes': sorted(missing - known),
        'evidence_disagrees_minutes': sorted(known - missing),
        'evidence': evidence, 'source': EVIDENCE['source'] if evidence else None,
        'provenance': EVIDENCE['provenance'] if evidence else None,
        'note': 'Chart absence corroboration does not establish delivery success or why the feed omitted a minute. Continuous coverage remains unchanged.'}


def legacy_trace(signal, coverage, envelopes, link, research_bars):
    if signal['script_version'] != '1.2.0': return None
    start = signal['signal_at_ms']
    missing = set(coverage['unreceived_elapsed_minutes'])
    receipts, packet_times = [], set()
    for row in envelopes:
        event = row['payload']['payload']
        if event['event_type'] != 'checkpoint': continue
        bars = event.get('stock_bars') or []
        packet_times.update(b['open_at_ms'] for b in bars)
        observation = event['observation']
        receipts.append({'event_id': event['event_id'], 'sha256': row['sha256'],
            'source_received_at': row['source_received'], 'horizon_min': observation['horizon_min'],
            'observed_at_ms': observation['observed_at_ms'], 'schema_version': event['schema_version'],
            'candle_count': len(bars), 'last_candle_open_ms': max((b['open_at_ms'] for b in bars), default=None)})
    match = bool(link and all(link['session'][k] == signal[k] for k in
        ('ticker', 'stream_id', 'script_version', 'logic_mode', 'timeframe_min', 'settings', 'is_test'))
        and link['record']['source_signal_id'] == signal['signal_id']
        and link['record']['at_ms'] == start and link['record']['price'] == signal['price'])
    research_times = {b['open_at_ms'] for b in research_bars} if match else set()
    native = sorted(m for m in missing if start+(m-1)*60000 in packet_times)
    research = sorted(m for m in missing if start+(m-1)*60000 in research_times)
    horizons = sorted({r['horizon_min'] for r in receipts})
    last = max((r['last_candle_open_ms'] for r in receipts if r['last_candle_open_ms'] is not None), default=None)
    return {'checkpoint_receipts': sorted(receipts, key=lambda r:r['horizon_min']),
        'retained_horizons': horizons, 'missing_horizons': [h for h in (5,15,30,60) if h not in horizons],
        'last_packet_candle_open_ms': last, 'native_packet_recoverable_minutes': native,
        'research_only_available_minutes': research,
        'not_in_retained_packets_or_matching_research': sorted(missing-set(native)-set(research)),
        'classification': 'native_packet_import_mismatch' if native else 'no_retained_checkpoint' if not receipts
            else 'retained_60m_packet_has_gaps' if 60 in horizons else 'checkpoint_receipts_end_before_60m',
        'research_identity_match': match,
        'note': 'v1.2 resends accumulated native candles at 5/15/30/60m. Absent packets do not prove delivery failure. Research candles retain separate provenance; no native evidence is filled.'}
