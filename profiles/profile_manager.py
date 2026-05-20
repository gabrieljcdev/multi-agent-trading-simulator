"""
profiles/profile_manager.py
Load, apply, and switch between risk profiles at runtime.
Profiles override settings.py values without editing code.
"""

import json
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)
PROFILES_DIR = Path(__file__).parent


@dataclass
class Profile:
    name:                   str
    description:            str

    # Position sizing
    max_position_size_pct:  float   # Max % of portfolio per trade
    max_open_positions:     int

    # Signal quality gate
    signal_score_threshold: float
    require_multi_tf:       bool

    # Risk
    default_stop_loss_pct:  float
    default_take_profit_pct: float
    daily_loss_limit_pct:   float
    consecutive_loss_limit: int

    # Strategy preferences
    preferred_strategy:     str
    arb_priority:           bool     # Prioritise arb signals

    # Sentiment influence
    sentiment_boost_amount:    float
    sentiment_suppress_amount: float

    # Claude behaviour
    claude_temperature:     float


class ProfileManager:
    def __init__(self):
        self._current: Optional[Profile] = None
        self._profiles: dict[str, Profile] = {}
        self._load_all()

    def _load_all(self):
        for path in PROFILES_DIR.glob("*.json"):
            try:
                with open(path) as f:
                    data = json.load(f)
                profile = Profile(**data)
                self._profiles[profile.name] = profile
                logger.debug(f"Loaded profile: {profile.name}")
            except Exception as e:
                logger.warning(f"Failed to load profile {path.name}: {e}")

    def load(self, name: str) -> Profile:
        if name not in self._profiles:
            raise ValueError(f"Profile '{name}' not found. Available: {list(self._profiles.keys())}")
        self._current = self._profiles[name]
        logger.info(f"Active profile: {name}")
        return self._current

    @property
    def current(self) -> Profile:
        if self._current is None:
            return self.load("balanced")
        return self._current

    def list_profiles(self) -> list[str]:
        return list(self._profiles.keys())

    def save_custom(self, profile: Profile):
        path = PROFILES_DIR / "custom.json"
        with open(path, "w") as f:
            json.dump(profile.__dict__, f, indent=2)
        self._profiles["custom"] = profile
        logger.info("Custom profile saved")


# Singleton
profile_manager = ProfileManager()
