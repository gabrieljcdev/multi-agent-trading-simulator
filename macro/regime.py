"""
macro/regime.py

Dimensional flag enums + MacroRegime dataclass. The three-layer model:

  Layer 1: dimensional flags (DollarStrength, RiskAppetite,
           RateEnvironment, VolRegime) — orthogonal axes
  Layer 2: scenario label (MacroScenario) — derived from the flags
           via a priority-ordered ladder (CRISIS first, NEUTRAL last)
  Layer 3: numeric score (macro_score) — -100..+100 composite of
           weighted per-component scores

The monitor (macro/monitor.py) populates this dataclass from
data_sources values + DB history. Consumers (quality gate, dashboard,
future MacroAgent) read it as a single immutable snapshot.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Optional


class DollarStrength(enum.Enum):
    STRONG  = "STRONG"
    NEUTRAL = "NEUTRAL"
    WEAK    = "WEAK"


class RiskAppetite(enum.Enum):
    RISK_ON  = "RISK_ON"
    NEUTRAL  = "NEUTRAL"
    RISK_OFF = "RISK_OFF"


class RateEnvironment(enum.Enum):
    TIGHTENING = "TIGHTENING"
    NEUTRAL    = "NEUTRAL"
    EASING     = "EASING"


class VolRegime(enum.Enum):
    CALM     = "CALM"
    ELEVATED = "ELEVATED"
    CRISIS   = "CRISIS"


class MacroScenario(enum.Enum):
    """Single-label summary of the regime. Priority-ordered: most
    severe scenarios are tested first in MacroMonitor.scenario_for()
    so they win when multiple labels could apply."""
    CRISIS           = "CRISIS"
    RISK_OFF         = "RISK_OFF"
    STAGFLATION      = "STAGFLATION"
    TIGHTENING_CYCLE = "TIGHTENING_CYCLE"
    EASING_CYCLE     = "EASING_CYCLE"
    REFLATION        = "REFLATION"
    GOLDILOCKS       = "GOLDILOCKS"
    NEUTRAL          = "NEUTRAL"


@dataclass
class MacroRegime:
    """One snapshot of the macro environment."""
    scenario:        MacroScenario
    dollar:          DollarStrength
    risk:            RiskAppetite
    rates:           RateEnvironment
    vol:             VolRegime
    macro_score:     float                  # -100..+100

    dxy:             Optional[float] = None
    vix:             Optional[float] = None
    yield_10y:       Optional[float] = None
    yield_2y:        Optional[float] = None
    yield_curve:     Optional[float] = None  # 10y - 2y spread
    fed_funds_rate:  Optional[float] = None
    cpi_yoy:         Optional[float] = None

    last_updated:    float           = 0.0   # epoch seconds
    confidence:      float           = 0.0   # 0..1

    raw_data:        dict            = field(default_factory=dict)

    def __post_init__(self):
        if self.last_updated == 0.0:
            self.last_updated = time.time()
        # Clamp to declared bounds so downstream math is always safe.
        self.macro_score = max(-100.0, min(100.0, float(self.macro_score)))
        self.confidence  = max(0.0,   min(1.0,   float(self.confidence)))

    @property
    def is_hard_block(self) -> bool:
        """CRISIS scenario short-circuits every signal — same shape as
        the sentiment news guard."""
        return self.scenario is MacroScenario.CRISIS
