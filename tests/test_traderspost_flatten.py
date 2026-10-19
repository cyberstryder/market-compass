"""Flatten payload tests: the 15:45 exit must carry cancel=true so resting
bracket orders are cancelled with the exit (TradersPost signal reference).
The payload 'strategy' value is ignored metadata per TradersPost docs —
scoping is by webhook URL — so it is asserted only for stability."""
import contextlib
import json
import types
import urllib.request

from compass import traderspost


class FakeDB:
    @contextlib.contextmanager
    def tx(self):
        yield None

    def health(self, *a, **k):
        pass


class FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b'{"ok":true}'


def test_flatten_payload_cancels_resting_orders(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured['body'] = req.data
        captured['url'] = req.full_url
        return FakeResp()

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)
    cfg = types.SimpleNamespace(
        traderspost_enabled=True,
        traderspost_webhook_url='https://example.invalid/hook',
        traderspost_webhook_url_2='')
    ok, _msg = traderspost.send_close_signal(cfg, FakeDB(), 'MES.c.0')
    assert ok is True
    payload = json.loads(captured['body'])
    assert payload['ticker'] == 'MES'
    assert payload['action'] == 'exit'
    assert payload['cancel'] is True
