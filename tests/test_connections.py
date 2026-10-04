import asyncio
import httpx
import pytest
from compass.alerts import deliver
from compass.config import Config
from compass.diagnostics import redacted_detail
from compass.providers import Collectors, FeedError
from compass.store import Store


def test_provider_diagnostic_keeps_reason_without_credentials():
    key = "test-secret-for-provider"
    value = f"Authentication failed for {key}; sk-proj-abc***xyz db-abc***xyz td_live_example https://provider.example/?apiKey={key} trader@example.com"
    detail = redacted_detail(value, (key,))
    assert "Authentication failed" in detail
    for private in (key, "sk-proj", "db-abc", "td_live", "apiKey=", "trader@example.com"):
        assert private not in detail
    assert len(redacted_detail("x" * 2000)) == 500


def test_databento_auth_error_surfaces_without_key(tmp_path, monkeypatch):
    import databento
    key = "fake-databento-secret"

    def reject(**kwargs):
        raise databento.BentoError("Authentication failed: " + key)

    monkeypatch.setattr(databento, "Live", reject)
    db = Store("sqlite:///" + str(tmp_path / "futures.db"))
    db.initialize()

    async def run():
        collector = Collectors(db, Config(local=True, databento=key))
        try:
            with pytest.raises(FeedError, match="Authentication failed") as error:
                await collector.futures()
            assert key not in str(error.value)
        finally:
            await collector.close()

    asyncio.run(run())
    db.engine.dispose()


def test_discord_verifies_empty_outbox_without_posting(tmp_path, monkeypatch):
    db = Store("sqlite:///" + str(tmp_path / "alerts.db"))
    db.initialize()
    db.health("discord", "not_configured", "Previous deployment")
    requests = []

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url):
            requests.append(("GET", url))
            return httpx.Response(200, json={"type": 1})
        async def post(self, *args, **kwargs):
            pytest.fail("No synthetic Discord message may be posted")

    async def end_wait(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr("compass.alerts.httpx.AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr("compass.alerts.asyncio.sleep", end_wait)
    cfg = Config(local=True, discord="https://discord.com/api/webhooks/123/fake-token?thread_id=456")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(deliver(db, cfg))
    with db.tx() as c:
        assert db.get(c, "health:discord")["status"] == "connected"
        assert db.get(c, "outbox:discord") is None
        assert db.recent(c, "alert") == []
    assert requests == [("GET", "https://discord.com/api/webhooks/123/fake-token")]
    db.engine.dispose()
