"""Tests for compass/iv_rank.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from compass.iv_rank import describe


def test_describe_high():
    assert "high" in describe({"iv_rank": 85})
    assert "selling premium" in describe({"iv_rank": 85})


def test_describe_low():
    assert "low" in describe({"iv_rank": 15})
    assert "buying premium" in describe({"iv_rank": 15})


def test_describe_mid():
    assert "mid" in describe({"iv_rank": 50})


def test_describe_missing():
    assert describe({}) == "IV rank unavailable"
    assert describe(None) == "IV rank unavailable"
    assert describe({"iv_rank": None}) == "IV rank unavailable"


def test_describe_boundaries():
    assert "high" in describe({"iv_rank": 70})
    assert "low" in describe({"iv_rank": 30})
    assert "mid" in describe({"iv_rank": 69})
    assert "mid" in describe({"iv_rank": 31})
