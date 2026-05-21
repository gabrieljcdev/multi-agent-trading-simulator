"""
tests/test_quality_gate.py — QualityGate integration tests.

Coverage focuses on wires the gate consumes:
  - sentiment composite + hard block flow into the gate
  - SENTIMENT_HARD_BLOCK skip_reason fires correctly
  - macro modifier already covered in test_macro.py; this file only
    tests the new sentiment wiring

Every test stubs the upstream collaborators (regime detector, guards,
OFI, macro) so the assertion is about one isolated path through the
gate.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from config import settings
from signals.base import Signal
from signals.quality_gate import QualityGate


def _make_signal(direction: str = "long", raw_score: float = 80.0) -> Signal:
    return Signal(
        pair="BTC/USDT", signal_type="momentum", direction=direction,
        exchange="binance", raw_score=raw_score, rsi=50.0,
    )


def _patch_neutral_environment(direction="long"):
    """Helpers: bypass everything except the sentiment path so each
    test exercises one wire in isolation."""
    return (
        patch("signals.quality_gate.regime_detector.get_primary", return_value=None),
        patch("signals.quality_gate.guard_runner.apply_all", return_value=(0.0, [])),
        patch("signals.ofi.ofi_scorer.get_best", return_value=None),
        patch("macro.macro_monitor.get_signal_modifier", return_value=0),
        patch("macro.macro_monitor.get_current_regime", return_value=None),
    )


def test_sentiment_hard_block_skips_with_skip_reason(monkeypatch):
    """A news-guard event in the sentiment aggregator must short-circuit
    the gate with the canonical SENTIMENT_HARD_BLOCK skip_reason."""
    monkeypatch.setattr(
        "sentiment.sentiment.is_hard_blocked",
        lambda: (True, "major_exchange_hack"),
    )
    # Modifier irrelevant — block fires first. Stub anyway so the gate
    # doesn't accidentally fall through to the field.
    monkeypatch.setattr(
        "sentiment.sentiment.get_signal_modifier",
        lambda: 0,
    )

    g = QualityGate()
    with patch("signals.quality_gate.regime_detector.get_primary", return_value=None), \
         patch("signals.quality_gate.guard_runner.apply_all", return_value=(0.0, [])), \
         patch("signals.ofi.ofi_scorer.get_best", return_value=None):
        s = _make_signal()
        s.tf_5m = s.tf_15m = s.tf_1h = True
        passed, reasons, _ = g.evaluate(s, [], [])

    assert passed is False
    assert any(settings.SENTIMENT_HARD_BLOCK_SKIP_REASON in r for r in reasons)


def test_sentiment_composite_modifier_applied(monkeypatch):
    """When aggregator returns a non-zero modifier the gate must add it
    to the final score (no longer reading signal.sentiment_mod alone)."""
    monkeypatch.setattr(
        "sentiment.sentiment.is_hard_blocked",
        lambda: (False, ""),
    )
    monkeypatch.setattr(
        "sentiment.sentiment.get_signal_modifier",
        lambda: -10,
    )

    g = QualityGate()
    with patch("signals.quality_gate.regime_detector.get_primary", return_value=None), \
         patch("signals.quality_gate.guard_runner.apply_all", return_value=(0.0, [])), \
         patch("signals.ofi.ofi_scorer.get_best", return_value=None), \
         patch("macro.macro_monitor.get_signal_modifier", return_value=0), \
         patch("macro.macro_monitor.get_current_regime", return_value=None):
        s = _make_signal(raw_score=80.0)
        s.tf_5m = s.tf_15m = s.tf_1h = True
        passed, _, score = g.evaluate(s, [], [])

    assert score == pytest.approx(70.0)         # 80 - 10
    assert passed is True


def test_sentiment_default_zero_when_aggregator_blows_up(monkeypatch):
    """Aggregator raising must fall through to whatever the scanner
    pre-populated on signal.sentiment_mod (legacy path), not crash."""
    monkeypatch.setattr(
        "sentiment.sentiment.is_hard_blocked",
        lambda: (_ for _ in ()).throw(RuntimeError("aggregator offline")),
    )

    g = QualityGate()
    with patch("signals.quality_gate.regime_detector.get_primary", return_value=None), \
         patch("signals.quality_gate.guard_runner.apply_all", return_value=(0.0, [])), \
         patch("signals.ofi.ofi_scorer.get_best", return_value=None), \
         patch("macro.macro_monitor.get_signal_modifier", return_value=0), \
         patch("macro.macro_monitor.get_current_regime", return_value=None):
        s = _make_signal()
        s.tf_5m = s.tf_15m = s.tf_1h = True
        s.sentiment_mod = -5.0       # legacy pre-set by scanner
        passed, _, score = g.evaluate(s, [], [])

    # Aggregator error → use legacy field. 80 - 5 = 75.
    assert score == pytest.approx(75.0)
    assert passed is True
