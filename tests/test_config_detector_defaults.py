"""Pin ICT detector config defaults to production reality (2026-10-19).

Before the cleanup, every detector defaulted to OFF in code while
production ran them via Railway env vars — losing one env var in a
redeploy would silently disable live detectors. Defaults now mirror the
detectors with verified production paper trades (/api/simple drills);
env vars remain as overrides. Deliberately-off detectors stay off.
"""
import pytest

from compass.config import Config

ICT_VARS = [
    'ICT_TURTLE_SOUP_ENABLED', 'ICT_SMT_DIVERGENCE_ENABLED',
    'ICT_AOI_ZONES_ENABLED', 'ICT_AOI_FADE_ENABLED',
    'ICT_GOLDEN_ZONE_ENABLED', 'ICT_BOS_FVG_ENABLED',
    'ICT_BOS_GZ_VWAP_ENABLED', 'ICT_RUMERS_BOX_ENABLED',
    'ICT_FUTURES_PAPER_ENABLED', 'ICT_CONTINUATION_ENABLED',
    'ICT_TREND_RIDER_ENABLED', 'ICT_MORNING_DRIVE_ENABLED',
    'ICT_ICC_ENABLED',
]


@pytest.fixture
def clean_env(monkeypatch):
    for var in ICT_VARS:
        monkeypatch.delenv(var, raising=False)


def test_production_detectors_default_on(clean_env):
    cfg = Config()
    assert cfg.ict_futures_paper is True
    assert cfg.ict_turtle_soup is True
    assert cfg.ict_smt_divergence is True
    assert cfg.ict_aoi_zones is True
    assert cfg.ict_aoi_fade is True
    assert cfg.ict_golden_zone is True
    assert cfg.ict_bos_fvg is True
    assert cfg.ict_bos_gz_vwap is True
    assert cfg.ict_rumers_box is True


def test_deliberately_off_detectors_stay_off(clean_env):
    cfg = Config()
    assert cfg.ict_continuation is False  # Josh disabled, 2026-10-14
    assert cfg.ict_trend_rider is False   # default-off by design, 2026-10-08
    assert cfg.ict_gz_ema_cross is False  # contradicted the GZ setup


def test_env_var_still_overrides_default(clean_env, monkeypatch):
    monkeypatch.setenv('ICT_SMT_DIVERGENCE_ENABLED', 'false')
    monkeypatch.setenv('ICT_CONTINUATION_ENABLED', 'true')
    cfg = Config()
    assert cfg.ict_smt_divergence is False
    assert cfg.ict_continuation is True


RETIRED_VARS = ['MORNING_ORB_ENABLED', 'ORB_SETUPS_ENABLED', 'VWAP_FADE_ENABLED']


def test_retired_programs_default_off(monkeypatch):
    """Morning/ORB retired (ops/ORB_RETIREMENT.md) and VWAP fade cannot
    submit trades: all three default off as of 2026-10-23; env still overrides."""
    for var in RETIRED_VARS:
        monkeypatch.delenv(var, raising=False)
    cfg = Config()
    assert cfg.morning_enabled is False
    assert cfg.orb_setups is False
    assert cfg.vwap_fade_enabled is False
    monkeypatch.setenv('MORNING_ORB_ENABLED', 'true')
    assert Config().morning_enabled is True


def test_extra_futures_rejects_silver(monkeypatch):
    """SIL/SI dropped 2026-10-15: Tradeify does not support SIL."""
    # Repo convention: local mode so validate() reaches the futures
    # checks instead of stopping at the production-Postgres guard.
    monkeypatch.setenv('COMPASS_LOCAL', 'true')
    monkeypatch.setenv('EXTRA_FUTURES_SYMBOLS', 'MGC.v.0,SIL.v.0')
    with pytest.raises(ValueError, match='Additional futures'):
        Config().validate()
    monkeypatch.setenv('EXTRA_FUTURES_SYMBOLS', 'MGC.v.0,MCL.v.0')
    Config().validate()
