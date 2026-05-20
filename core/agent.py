"""
core/agent.py
Claude agent — evaluates signals and generates trade recommendations.
"""
import logging
import os
import json
from datetime import datetime
from typing import Optional
import anthropic
from dotenv import load_dotenv
from signals.base import Signal
from database.queries import get_signal_history, get_signal_win_rate
from config import settings

load_dotenv("config/keys.env")
logger = logging.getLogger(__name__)

class ClaudeAgent:
    def __init__(self):
        self._client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self._daily_cost = 0.0
        self._call_count = 0

    async def evaluate_signal(self, signal: Signal, regime_snap=None, ofi_snap=None) -> Signal:
        """Evaluate a signal and populate Claude's recommendation fields."""
        try:
            brief = self._build_brief(signal, regime_snap, ofi_snap)
            response = self._client.messages.create(
                model=settings.CLAUDE_MODEL,
                max_tokens=settings.CLAUDE_MAX_TOKENS,
                temperature=settings.CLAUDE_TEMPERATURE,
                messages=[{"role": "user", "content": brief}]
            )
            text = response.content[0].text
            cost = self._estimate_cost(response)
            self._daily_cost += cost
            self._call_count += 1
            signal.claude_api_cost = cost
            signal = self._parse_response(signal, text)
            logger.info(f"Claude evaluated {signal.pair} — cost ${cost:.4f}")
            return signal
        except Exception as e:
            logger.error(f"Claude agent error: {e}")
            signal.claude_reasoning = f"Evaluation failed: {e}"
            return signal

    def _build_brief(self, signal: Signal, regime_snap=None, ofi_snap=None) -> str:
        lines = []
        lines.append("You are a crypto trading analyst. Evaluate this signal and provide a trade recommendation.")
        lines.append(f"\n## Signal")
        lines.append(f"Pair: {signal.pair}")
        lines.append(f"Type: {signal.signal_type.upper()}")
        lines.append(f"Direction: {signal.direction.upper()}")
        lines.append(f"Score: {signal.score:.0f}/100")
        lines.append(f"Exchange: {signal.exchange}")
        if signal.exchange_b:
            lines.append(f"Exchange B: {signal.exchange_b}")

        lines.append(f"\n## Technical Indicators")
        if signal.rsi:
            lines.append(f"RSI: {signal.rsi:.1f}")
        if signal.volume_ratio:
            lines.append(f"Volume ratio: {signal.volume_ratio:.1f}x average")
        if signal.bb_position is not None:
            lines.append(f"BB position: {signal.bb_position:.2f} (0=lower band, 1=upper band)")
        if signal.arb_gap_pct:
            lines.append(f"Arb gap: {signal.arb_gap_pct:.3f}% after fees")
        if signal.macd_hist:
            lines.append(f"MACD histogram: {signal.macd_hist:.4f}")

        lines.append(f"\n## Timeframe Confirmations")
        lines.append(f"5m: {'✓' if signal.tf_5m else '✗'}")
        lines.append(f"15m: {'✓' if signal.tf_15m else '✗'}")
        lines.append(f"1h: {'✓' if signal.tf_1h else '✗'}")

        if regime_snap:
            lines.append(f"\n## Market Regime")
            lines.append(f"Regime: {regime_snap.regime.upper()}")
            lines.append(f"ADX: {regime_snap.adx:.1f}" if regime_snap.adx else "ADX: N/A")
            lines.append(f"Hurst: {regime_snap.hurst:.2f} ({regime_snap.hurst_label})" if regime_snap.hurst else "Hurst: N/A")
            lines.append(f"ATR percentile: {regime_snap.atr_percentile:.0f}th" if regime_snap.atr_percentile else "")

        if ofi_snap:
            lines.append(f"\n## Order Flow")
            lines.append(f"OFI: {ofi_snap.ema_ofi:.2f} ({ofi_snap.direction})")
            if ofi_snap.vpin:
                lines.append(f"VPIN: {ofi_snap.vpin:.2f}")

        lines.append(f"\n## Sentiment")
        lines.append(f"Composite score: {signal.sentiment_score:.0f}/100")
        lines.append(f"Velocity: {signal.sentiment_velocity:+.1f} over 2h")
        lines.append(f"Sentiment modifier applied: {signal.sentiment_mod:+.1f}")

        # Historical context
        if settings.CLAUDE_CONTEXT.get("include_past_similar"):
            try:
                stats = get_signal_win_rate(signal.signal_type, days=30)
                if stats["total"] > 0:
                    lines.append(f"\n## Historical Performance (last 30 days)")
                    lines.append(f"Similar signals: {stats['total']}")
                    lines.append(f"Win rate: {stats['win_rate']*100:.0f}%")
                    lines.append(f"Avg P&L: {stats['avg_pnl']*100:.2f}%")
            except:
                pass

        lines.append(f"\n## Your Task")
        lines.append("1. Assess whether this is a good trade given all context above")
        lines.append("2. Provide your reasoning in 2-3 sentences")
        lines.append("3. Suggest entry, stop loss, take profit, and position size (% of portfolio)")
        lines.append("4. End with RECOMMENDATION: GO or RECOMMENDATION: SKIP")
        lines.append("\nBe concise. Focus on the key factors. Flag any risks.")
        lines.append(f"\nProfile: {settings.ACTIVE_PROFILE} | Max position: {settings.MAX_POSITION_SIZE_PCT*100:.0f}% | Default SL: {settings.DEFAULT_STOP_LOSS_PCT*100:.1f}% | Default TP: {settings.DEFAULT_TAKE_PROFIT_PCT*100:.1f}%")

        return "\n".join(lines)

    def _parse_response(self, signal: Signal, text: str) -> Signal:
        signal.claude_reasoning = text
        # Parse recommendation
        text_upper = text.upper()
        if "RECOMMENDATION: SKIP" in text_upper:
            signal.indicators["claude_rec"] = "SKIP"
        elif "RECOMMENDATION: GO" in text_upper:
            signal.indicators["claude_rec"] = "GO"
        else:
            signal.indicators["claude_rec"] = "UNCLEAR"
        # Try to extract numbers
        import re
        # Entry
        if not signal.suggested_entry:
            m = re.search(r'entry[:\s]+\$?([\d,.]+)', text, re.IGNORECASE)
            if m:
                try: signal.suggested_entry = float(m.group(1).replace(',',''))
                except: pass
        # Stop loss
        if not signal.suggested_sl:
            m = re.search(r'stop.loss[:\s]+\$?([\d,.]+)', text, re.IGNORECASE)
            if m:
                try: signal.suggested_sl = float(m.group(1).replace(',',''))
                except: pass
        # Take profit
        if not signal.suggested_tp:
            m = re.search(r'take.profit[:\s]+\$?([\d,.]+)', text, re.IGNORECASE)
            if m:
                try: signal.suggested_tp = float(m.group(1).replace(',',''))
                except: pass
        return signal

    def _estimate_cost(self, response) -> float:
        try:
            input_tokens  = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            return (input_tokens * 0.000003) + (output_tokens * 0.000015)
        except:
            return 0.001

    async def self_review(self, trade) -> str:
        """Post-trade review — Claude analyses what went right or wrong."""
        if not settings.SELF_REVIEW_ENABLED:
            return ""
        try:
            outcome = "WIN" if trade.pnl_pct and trade.pnl_pct > 0 else "LOSS"
            prompt = f"""Brief post-trade review (2-3 sentences max):
Trade: {trade.pair} {trade.side} {outcome}
P&L: {trade.pnl_pct*100:.2f}% | Hold: {trade.hold_minutes:.0f} min
Exit reason: {trade.exit_reason}
Original signal score: {trade.signal.score if hasattr(trade, 'signal') else 'N/A'}

What went right or wrong? What should be weighted differently next time?"""
            response = self._client.messages.create(
                model=settings.CLAUDE_MODEL,
                max_tokens=settings.SELF_REVIEW_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.content[0].text
        except Exception as e:
            logger.error(f"Self review error: {e}")
            return ""

    @property
    def daily_cost(self) -> float:
        return self._daily_cost

    def reset_daily_cost(self):
        self._daily_cost = 0.0
        self._call_count = 0

agent = ClaudeAgent()
