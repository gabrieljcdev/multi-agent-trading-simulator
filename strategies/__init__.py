"""
strategies/__init__.py
Strategy registry. Add new strategies here.
"""

from .default import DefaultStrategy
from .arb_only import ArbOnlyStrategy
from .scalper import ScalperStrategy
from .custom import CustomStrategy
from .base_strategy import BaseStrategy

STRATEGIES: dict[str, type[BaseStrategy]] = {
    "default":  DefaultStrategy,
    "arb_only": ArbOnlyStrategy,
    "scalper":  ScalperStrategy,
    "custom":   CustomStrategy,
}

def get_strategy(name: str) -> BaseStrategy:
    cls = STRATEGIES.get(name)
    if cls is None:
        raise ValueError(f"Unknown strategy '{name}'. Available: {list(STRATEGIES.keys())}")
    return cls()
