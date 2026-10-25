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
    'ICT_FUTURES_PAPER_ENABLED', 'ICT_TREND_RIDER_ENABLED',
]


@pytest.fixture
def clean_env(monkeypatch):
    for var in ICT_VARS:
        monkeypatch.delenv(var, raising=False)


def test_production_detectors_default_on(clean_env):
    cfg = Config()
    assert cfg.ict_futures_paper is True
    assert cfg.ict_turtle_soup is True
    assert cfg.ict_aoi_fade is True
    assert cfg.ict_golden_zone is True
    assert cfg.ict_rumers_box is True


def test_deliberately_off_detectors_stay_off(clean_env):
    cfg = Config()
    # ICT Continuation is not asserted here: the detector was retired
    # outright 2026-10-25 (module deleted; see docs/RETIRED_DETECTORS.md).
    assert cfg.ict_trend_rider is False   # default-off by design, 2026-10-08
    assert cfg.ict_gz_ema_cross is False  # contradicted the GZ setup
    # Futures prune (Josh, 2026-10-24): structure-chasers default off;
    # env vars can still re-enable them deliberately.
    assert cfg.ict_bos_fvg is False
    assert cfg.ict_bos_gz_vwap is False
    assert cfg.ict_aoi_zones is False
    assert cfg.ict_smt_divergence is False


def test_env_var_still_overrides_default(clean_env, monkeypatch):
    monkeypatch.setenv('ICT_SMT_DIVERGENCE_ENABLED', 'false')
    monkeypatch.setenv('ICT_TREND_RIDER_ENABLED', 'true')
    cfg = Config()
    assert cfg.ict_smt_divergence is False
    assert cfg.ict_trend_rider is True


RETIRED_VARS = ['MORNING_ORB_ENABLED', 'ORB_SETUPS_ENABLED']


def test_retired_programs_default_off(monkeypatch):
    """Morning/ORB retired (ops/ORB_RETIREMENT.md): both default off as
    of 2026-10-23; env still overrides. VWAP_FADE_ENABLED was in this
    list until 2026-10-25, when the VWAP Fade detector was deleted
    outright (docs/RETIRED_DETECTORS.md) and its config fields removed."""
    for var in RETIRED_VARS:
        monkeypatch.delenv(var, raising=False)
    cfg = Config()
    assert cfg.morning_enabled is False
    assert cfg.orb_setups is False
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
