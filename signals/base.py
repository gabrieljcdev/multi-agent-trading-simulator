"""
signals/base.py
Signal dataclass — the standard object that flows through the entire system.
Every signal type (arb, momentum, reversion) produces one of these.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Signal:
    # Core identity
    pair:         str
    signal_type:  str          # arb | momentum | reversion
    direction:    str          # long | short | arb
    exchange:     str
    exchange_b:   Optional[str] = None  # For arb: the second exchange

    # Scoring
    raw_score:       float = 0.0
    sentiment_mod:   float = 0.0

    @property
    def score(self) -> float:
        return min(100.0, max(0.0, self.raw_score + self.sentiment_mod))

    # Key indicators at signal time
    rsi:            Optional[float] = None
    macd_hist:      Optional[float] = None
    bb_position:    Optional[float] = None  # 0=lower, 0.5=mid, 1=upper
    volume_ratio:   Optional[float] = None  # vs 20-period avg
    arb_gap_pct:    Optional[float] = None  # For arb signals

    # Timeframe confirmations
    tf_5m:  bool = False
    tf_15m: bool = False
    tf_1h:  bool = False

    @property
    def timeframe_confirmations(self) -> int:
        return sum([self.tf_5m, self.tf_15m, self.tf_1h])

    # Sentiment context
    sentiment_score:    float = 50.0
    sentiment_velocity: float = 0.0

    # Full indicator snapshot for DB storage and ML
    indicators: dict = field(default_factory=dict)

    # Timestamps
    created_at:   datetime = field(default_factory=datetime.utcnow)
    expires_at:   Optional[datetime] = None

    # Claude's analysis (populated after evaluation)
    claude_reasoning:     Optional[str]   = None
    suggested_entry:      Optional[float] = None
    suggested_sl:         Optional[float] = None
    suggested_tp:         Optional[float] = None
    suggested_size_pct:   Optional[float] = None  # % of portfolio
    risk_reward:          Optional[float] = None
    claude_api_cost:      float = 0.0

    # Predictive model output (populated if model is active)
    win_probability:      Optional[float] = None

    # DB id (set after saving)
    db_id: Optional[int] = None

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.utcnow() > self.expires_at

    def summary(self) -> str:
        """One-line summary for logging."""
        return (
            f"{self.signal_type.upper()} {self.pair} {self.direction.upper()} "
            f"score={self.score:.0f} rsi={self.rsi:.0f if self.rsi else 'n/a'}"
        )

    def to_db_dict(self) -> dict:
        """Serialise for database storage."""
        return {
            "pair":              self.pair,
            "exchange":          self.exchange,
            "exchange_b":        self.exchange_b,
            "signal_type":       self.signal_type,
            "direction":         self.direction,
            "score":             self.score,
            "passed_gate":       True,
            "rsi":               self.rsi,
            "macd_hist":         self.macd_hist,
            "bb_position":       self.bb_position,
            "volume_ratio":      self.volume_ratio,
            "arb_gap_pct":       self.arb_gap_pct,
            "sentiment_score":   self.sentiment_score,
            "sentiment_mod":     self.sentiment_mod,
            "sentiment_velocity": self.sentiment_velocity,
            "tf_5m_confirm":     self.tf_5m,
            "tf_15m_confirm":    self.tf_15m,
            "tf_1h_confirm":     self.tf_1h,
            "indicators_json":   self.indicators,
            "claude_reasoning":  self.claude_reasoning,
            "claude_suggested_entry": self.suggested_entry,
            "claude_suggested_sl":    self.suggested_sl,
            "claude_suggested_tp":    self.suggested_tp,
            "claude_suggested_size":  self.suggested_size_pct,
            "claude_risk_reward":     self.risk_reward,
            "claude_api_cost_usd":    self.claude_api_cost,
            "timestamp":         self.created_at,
        }
