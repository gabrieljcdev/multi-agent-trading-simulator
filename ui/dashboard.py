"""
ui/dashboard.py

Rich terminal dashboard — single pane of glass for CryptoBot.

Twelve stacked rows: header, portfolio bar, status/regime/sentiment/macro,
market overview, agents/circuit-breakers/top-performers/events, positions
table, signal feed + arb feed + arb opportunity stats, exchange health +
session performance, log feed + approval panel, insights strip, footer,
command bar.

Every panel is wrapped in try/except — a broken data source must never
crash the whole dashboard. Modules that haven't been built yet (macro,
calendar, agent coordinator) render as "—" or "Not yet built".

Construct with `Dashboard(bot)`. Optional `coordinator=` will surface
multi-agent portfolio stats once that layer exists.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Colour helpers — every colour comes through here so it's easy to retheme
# ─────────────────────────────────────────────────────────────────────────

def _pnl_colour(pnl: float) -> str:
    if pnl > 0:
        return "bright_green"
    if pnl < 0:
        return "red"
    return "white"


def _regime_colour(regime: str) -> str:
    return {
        "trending": "bright_green",
        "ranging":  "cyan",
        "high_vol": "yellow",
        "choppy":   "red",
        "unknown":  "dim white",
    }.get((regime or "unknown").lower(), "white")


def _status_colour(status: str) -> str:
    return {
        "RUNNING": "bright_green",
        "PAUSED":  "yellow",
        "HALTED":  "red",
        "KILLED":  "red blink",
        "BLOCKED": "orange1",
        "OFFLINE": "dim white",
    }.get(status, "white")


def _fear_greed_colour(value: int) -> str:
    if value <= 25:
        return "red"
    if value <= 45:
        return "orange1"
    if value <= 55:
        return "yellow"
    if value <= 75:
        return "green"
    return "bright_green"


# ─────────────────────────────────────────────────────────────────────────
# Internal state shapes
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class _ExchangeHealth:
    latency_ms: float = 0.0
    connected:  bool  = False
    last_seen:  Optional[datetime] = None


# ─────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────

class Dashboard:
    """Rich-Live terminal dashboard for CryptoBot."""

    LOG_BUFFER_MAX     = 40
    SIGNAL_BUFFER_MAX  = 20
    ARB_BUFFER_MAX     = 15
    INSIGHT_BUFFER_MAX = 3

    def __init__(self, bot=None, coordinator=None):
        # Both are optional. With a coordinator, the primary bot can be
        # late-bound via set_bot() once the signal agent starts.
        self._bot         = bot
        self._coordinator = coordinator
        self._start_time  = datetime.utcnow()
        self._running     = False

        # Rolling buffers — push-only via the public add_* methods.
        self._log_buffer:     deque = deque(maxlen=self.LOG_BUFFER_MAX)
        self._signal_buffer:  deque = deque(maxlen=self.SIGNAL_BUFFER_MAX)
        self._arb_buffer:     deque = deque(maxlen=self.ARB_BUFFER_MAX)
        self._insight_buffer: deque = deque(maxlen=self.INSIGHT_BUFFER_MAX)

        # Pushed in via update_exchange_health()
        self._exchange_health: dict[str, _ExchangeHealth] = {}

        # 1–5 keyboard select; persists for filtering once agent layer exists.
        self._selected_agent: Optional[int] = None

        # Coordinator.get_*_stats() are async; render() is sync. The async
        # run() loop refreshes these caches so sync panels can read freely.
        self._portfolio_cache:   Optional[dict] = None
        self._agent_stats_cache: list[dict]     = []

        # Arb engine raw stats — AgentStats doesn't carry engine-specific
        # counters (missed_balance_checks, last_opportunity), so the
        # dashboard reaches through the coordinator's agent list to the
        # ArbEngine directly. Empty dict when no arb agent is present.
        self._arb_engine_stats_cache: dict = {}

        self._console = Console()

    # ── Push API ────────────────────────────────────────────────────────

    def add_log(self, msg: str, level: str = "INFO") -> None:
        self._log_buffer.append({
            "time":  datetime.utcnow().strftime("%H:%M:%S"),
            "level": level.upper(),
            "msg":   msg,
        })

    def add_signal(
        self,
        symbol:   str,
        track:    str,
        score:    float,
        regime:   str,
        action:   str,
        agent_id: str = "",
    ) -> None:
        self._signal_buffer.append({
            "time":   datetime.utcnow().strftime("%H:%M:%S"),
            "symbol": symbol,
            "track":  track,
            "score":  score,
            "regime": regime,
            "action": action.upper(),
            "agent":  agent_id,
        })

    def add_arb(
        self,
        pair:    str,
        buy_ex:  str,
        sell_ex: str,
        gap_pct: float,
        pnl:     float = 0.0,
    ) -> None:
        self._arb_buffer.append({
            "time":    datetime.utcnow().strftime("%H:%M:%S"),
            "pair":    pair,
            "buy":     buy_ex,
            "sell":    sell_ex,
            "gap_pct": gap_pct,
            "pnl":     pnl,
        })

    def add_insight(self, text: str) -> None:
        self._insight_buffer.append(text)

    def update_exchange_health(
        self,
        exchange:   str,
        latency_ms: float,
        connected:  bool,
    ) -> None:
        self._exchange_health[exchange] = _ExchangeHealth(
            latency_ms=latency_ms,
            connected=connected,
            last_seen=datetime.utcnow(),
        )

    def set_bot(self, bot) -> None:
        """Late-bind the primary bot.

        Used by the multi-agent coordinator path: at Dashboard construction
        time the signal agent's CryptoBot may not exist yet. The agent
        wires its bot in here once it's created so bot-specific panels
        (status, positions, approval, regime via market_data) can populate.
        """
        self._bot = bot

    # ── Layout build ────────────────────────────────────────────────────

    def render(self) -> Layout:
        """Build the full layout. Every panel call is wrapped per-row;
        a panel that raises is replaced with an error placeholder."""
        layout = Layout()
        layout.split_column(
            Layout(self._safe(self._panel_header,    "header"),    name="row1",  size=3),
            Layout(self._safe(self._panel_portfolio, "portfolio"), name="row2",  size=4),
            Layout(name="row3", size=11),
            Layout(self._safe(self._panel_market,    "market"),    name="row4",  size=6),
            Layout(name="row5", size=11),
            Layout(self._safe(self._panel_positions, "positions"), name="row6",  size=9),
            Layout(name="row7", size=11),
            Layout(name="row8", size=8),
            Layout(name="row9", size=12),
            Layout(self._safe(self._panel_insights,  "insights"),  name="row10", size=4),
            Layout(self._safe(self._panel_footer,    "footer"),    name="row11", size=4),
            Layout(self._safe(self._panel_cmd_bar,   "cmd_bar"),   name="row12", size=4),
        )

        layout["row3"].split_row(
            Layout(self._safe(self._panel_status,    "status")),
            Layout(self._safe(self._panel_regime,    "regime")),
            Layout(self._safe(self._panel_sentiment, "sentiment")),
            Layout(self._safe(self._panel_macro,     "macro")),
        )
        layout["row5"].split_row(
            Layout(self._safe(self._panel_agents,            "agents")),
            Layout(self._safe(self._panel_circuit_breakers,  "breakers")),
            Layout(self._safe(self._panel_top_performers,    "top")),
            Layout(self._safe(self._panel_pending_events,    "events")),
        )
        layout["row7"].split_row(
            Layout(self._safe(self._panel_signal_feed,         "signals"), ratio=2),
            Layout(self._safe(self._panel_arb_feed,            "arb"),     ratio=1),
            Layout(self._safe(self._panel_arb_opportunities,   "arb_opps"), ratio=1),
        )
        layout["row8"].split_row(
            Layout(self._safe(self._panel_exchange_health, "exchanges")),
            Layout(self._safe(self._panel_session_perf,    "session")),
        )
        layout["row9"].split_row(
            Layout(self._safe(self._panel_log_feed, "log")),
            Layout(self._safe(self._panel_approval, "approval")),
        )
        return layout

    def _safe(self, fn, name: str):
        """Run a panel-producing fn; on any error return a red error panel.

        Panels touch live data sources (DB, market_data, sentiment) — every
        one must degrade independently or the whole dashboard goes dark.
        """
        try:
            return fn()
        except Exception as e:
            logger.debug(f"dashboard panel {name}: {e}", exc_info=True)
            return Panel(f"[red]error: {e}[/red]", title=f"[red]{name}[/red]",
                         border_style="red")

    # ── Live driver ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Drive Rich.Live until stop() flips _running."""
        import asyncio
        self._running = True
        # screen=False so the user's stdin echoes are visible below the
        # dashboard (the alternate-screen buffer swallows them). The
        # cmd_bar panel echoes each submitted line on the next refresh.
        with Live(self.render(), refresh_per_second=2, screen=False,
                  console=self._console) as live:
            while self._running:
                await self._refresh_coordinator_data()
                try:
                    live.update(self.render())
                except Exception as e:
                    logger.debug(f"dashboard live update: {e}")
                await asyncio.sleep(0.5)

    async def _refresh_coordinator_data(self) -> None:
        """Pull async coordinator stats into sync-readable caches.

        Render panels are sync (Rich Live drives them), but the coordinator
        exposes async getters. Awaiting here once per tick keeps panels
        simple and avoids the unawaited-coroutine .get() crash.
        """
        if self._coordinator is None:
            return

        getter = getattr(self._coordinator, "get_portfolio_stats", None)
        if callable(getter):
            try:
                stats = await getter()
                if stats:
                    self._portfolio_cache = {
                        "total_equity":  stats.get("total_equity",       0.0),
                        "daily_pnl_pct": stats.get("total_daily_pnl_pct", 0.0),
                    }
            except Exception as e:
                logger.debug(f"dashboard portfolio refresh: {e}")

        getter = getattr(self._coordinator, "get_agent_stats", None)
        if callable(getter):
            try:
                stats_list = await getter() or []
                self._agent_stats_cache = [
                    {
                        "name":    (getattr(s, "agent_id", "?") or "?").title(),
                        "status":  getattr(s, "status", "OFFLINE"),
                        "capital": getattr(s, "capital_allocated", 0.0),
                        "pnl_pct": getattr(s, "daily_pnl_pct",     0.0),
                        "trades":  getattr(s, "trades_today",      0),
                    }
                    for s in stats_list
                ]
            except Exception as e:
                logger.debug(f"dashboard agent stats refresh: {e}")

        # Pull the arb engine's raw stats dict so the capital-gate line
        # and opportunity panel can read counters that AgentStats omits.
        # Wrapped defensively — coordinator's internal layout is private,
        # and the arb agent may not be registered in some configurations.
        try:
            agents = getattr(self._coordinator, "_agents", None) or []
            for a in agents:
                if getattr(a, "agent_id", None) != "arb":
                    continue
                engine = getattr(a, "_engine", None)
                if engine is None or not hasattr(engine, "get_stats"):
                    break
                self._arb_engine_stats_cache = engine.get_stats() or {}
                break
        except Exception as e:
            logger.debug(f"dashboard arb engine stats refresh: {e}")

    def stop(self) -> None:
        self._running = False

    # ════════════════════════════════════════════════════════════════════
    # Panels
    # ════════════════════════════════════════════════════════════════════

    def _panel_header(self) -> Panel:
        utc_now = datetime.utcnow()
        session = self._current_session(utc_now)
        session_colour = {
            "LONDON":    "bright_green",
            "NEW_YORK":  "bright_cyan",
            "ASIA":      "yellow",
            "OFF_HOURS": "dim white",
        }.get(session, "white")

        sim_live = "SIM" if settings.SIM_MODE else "LIVE"
        sim_colour = "yellow" if settings.SIM_MODE else "red bold"

        uptime = utc_now - self._start_time
        approval = getattr(settings, "APPROVAL_MODE", "?")

        t = Table.grid(expand=True)
        t.add_column(justify="left",   ratio=1)
        t.add_column(justify="center", ratio=1)
        t.add_column(justify="right",  ratio=1)
        t.add_row(
            f"[bold cyan]CryptoBot[/bold cyan] "
            f"[{sim_colour}]{sim_live}[/{sim_colour}]   "
            f"approval=[bold]{approval}[/bold]",
            f"[{session_colour}]{session}[/{session_colour}]   "
            f"[white]{utc_now.strftime('%Y-%m-%d %H:%M:%S')} UTC[/white]",
            f"uptime [bold]{self._fmt_duration(uptime)}[/bold]",
        )
        return Panel(t, border_style="cyan")

    def _panel_portfolio(self) -> Panel:
        cb = getattr(self._bot, "_cb_state", None)
        equity = getattr(cb, "current_equity", 0.0) if cb else 0.0
        daily  = getattr(cb, "daily_pnl_pct",  0.0) if cb else 0.0

        # Coordinator overrides bot stats if its cache is populated. The
        # async run() loop refreshes _portfolio_cache; we never call the
        # coroutine from this sync render path.
        portfolio = self._portfolio_cache
        if portfolio:
            equity = portfolio.get("total_equity",  equity)
            daily  = portfolio.get("daily_pnl_pct", daily)

        trades_today, _, _, wr_today, wr_all = self._read_trade_stats()
        open_exp = self._open_exposure_pct(equity)
        pnl_col = _pnl_colour(daily)

        t = Table.grid(expand=True)
        for _ in range(6):
            t.add_column(justify="center", ratio=1)
        t.add_row(
            f"[bold]Equity[/bold]\n${equity:,.2f}",
            f"[bold]Daily P&L[/bold]\n[{pnl_col}]{daily:+.2f}%[/{pnl_col}]",
            f"[bold]Open Exposure[/bold]\n{open_exp:.1f}%",
            (f"[bold]Win Rate Today[/bold]\n{wr_today*100:.0f}%"
             if wr_today is not None else "[bold]Win Rate Today[/bold]\n—"),
            (f"[bold]Win Rate All-Time[/bold]\n{wr_all*100:.0f}%"
             if wr_all is not None else "[bold]Win Rate All-Time[/bold]\n—"),
            f"[bold]Trades Today[/bold]\n{trades_today}",
        )
        return Panel(t, border_style="cyan", title="[bold]Portfolio[/bold]")

    def _panel_status(self) -> Panel:
        cb = getattr(self._bot, "_cb_state", None)
        if cb and getattr(cb, "halted", False):
            status = "HALTED"
        elif cb and cb.consecutive_losses >= settings.CIRCUIT_BREAKERS["consecutive_loss"]["count"] - 1:
            status = "BLOCKED"
        else:
            status = "RUNNING"

        colour = _status_colour(status)
        equity = getattr(cb, "current_equity", 0.0) if cb else 0.0
        daily  = getattr(cb, "daily_pnl_pct",  0.0) if cb else 0.0
        pnl_col = _pnl_colour(daily)

        trades_today, _, _, _, _ = self._read_trade_stats()
        fired = self._signals_fired_count()
        skipped = self._signals_skipped_count()

        body = Text()
        body.append("Status: ", style="bold")
        body.append(f"{status}\n", style=colour)
        body.append(f"Equity: ${equity:,.2f}\n")
        body.append("Daily P&L: ")
        body.append(f"{daily:+.2f}%\n", style=pnl_col)
        body.append(f"Trades today: {trades_today}\n")
        body.append(f"Signals fired: {fired}\n")
        body.append(f"Signals skipped: {skipped}\n")
        body.append(f"Last signal: {self._last_signal_time()}\n")
        return Panel(body, title="[bold]STATUS[/bold]", border_style="cyan")

    def _panel_regime(self) -> Panel:
        all_regimes = []
        try:
            from core.regime_detector import regime_detector
            all_regimes = regime_detector.all_regimes()
        except Exception:
            pass

        if not all_regimes:
            return Panel(
                "[dim]No regime data — start the bot[/dim]",
                title="[bold]REGIME[/bold]", border_style="cyan",
            )

        slow_tf = getattr(settings, "SLOW_TIMEFRAME", "1h")
        slow_snaps = [s for s in all_regimes if s.timeframe == slow_tf]
        if slow_snaps:
            counts = Counter(s.regime for s in slow_snaps)
            dominant, _ = counts.most_common(1)[0]
        else:
            dominant = "unknown"

        body = Text()
        body.append("Dominant: ", style="bold")
        body.append(f"{dominant.upper()}\n\n", style=_regime_colour(dominant))

        sub = Table.grid()
        sub.add_column()
        sub.add_column()
        sub.add_column()
        for snap in slow_snaps[:6]:
            c = _regime_colour(snap.regime)
            adx   = f"{snap.adx:.0f}"   if snap.adx   else "—"
            hurst = f"{snap.hurst:.2f}" if snap.hurst else "—"
            sub.add_row(
                f"{snap.pair:12s}",
                f"[{c}]{snap.regime}[/{c}]",
                f"ADX {adx}  H {hurst}",
            )
        return Panel(
            self._stack(body, sub),
            title="[bold]REGIME[/bold]", border_style="cyan",
        )

    def _panel_sentiment(self) -> Panel:
        latest = self._sentiment_latest()
        if not latest:
            return Panel(
                "[dim]Sentiment aggregator silent[/dim]",
                title="[bold]SENTIMENT[/bold]", border_style="cyan",
            )

        fg_val = latest.get("fear_greed_value")
        fg_lbl = latest.get("fear_greed_label", "—")

        body = Text()
        body.append("Fear & Greed: ", style="bold")
        if fg_val is not None:
            colour = _fear_greed_colour(fg_val)
            body.append(f"{fg_val} ({fg_lbl})\n", style=colour)
        else:
            body.append("—\n", style="dim")

        composite = latest.get("composite_score", 0.0)
        body.append(f"Composite: {composite:+.0f}\n")

        reddit = latest.get("reddit_score")
        if reddit is not None:
            body.append(f"Reddit: {reddit:+.0f}\n")
        else:
            body.append("Reddit: —\n", style="dim")

        news_active = latest.get("news_guard_active", False)
        body.append(
            f"News guard: {'ACTIVE' if news_active else 'clear'}\n",
            style="red bold" if news_active else "green",
        )

        btc_30m = latest.get("btc_change_30m")
        if btc_30m is not None:
            body.append(f"BTC 30m: {btc_30m:+.2f}%\n")
        else:
            body.append("BTC 30m: —\n", style="dim")

        for h in (latest.get("top_headlines") or [])[:2]:
            body.append(f"  • {h[:35]}\n", style="dim")

        return Panel(body, title="[bold]SENTIMENT[/bold]", border_style="cyan")

    def _panel_macro(self) -> Panel:
        """Render the MacroRegime — scenario label (colour-coded by
        severity), composite score bar, DXY/VIX/yield-curve/Fed/CPI,
        confidence. Reads macro_monitor; missing regime → dim panel
        with dashes."""
        try:
            from macro import macro_monitor
            regime = macro_monitor.get_current_regime()
        except Exception:
            regime = None

        if regime is None:
            body = Text()
            for label in ("Scenario", "Score", "DXY", "VIX",
                          "10y-2y", "Fed", "CPI YoY"):
                body.append(f"{label}: —\n", style="dim")
            body.append("\nwaiting for first refresh…",
                        style="dim italic")
            return Panel(body, title="[bold]MACRO[/bold]",
                         border_style="dim")

        # Scenario colour ladder — supportive green, neutral white,
        # rising severity yellow → orange → red.
        scenario_colour = {
            "GOLDILOCKS":       "bright_green",
            "EASING_CYCLE":     "green",
            "REFLATION":        "white",
            "NEUTRAL":          "white",
            "TIGHTENING_CYCLE": "yellow",
            "RISK_OFF":         "orange1",
            "STAGFLATION":      "orange1",
            "CRISIS":           "red blink",
        }.get(regime.scenario.name, "white")

        # VIX colour using the macro thresholds (matches the score curve).
        # Resolved as (text, style) — Text.append escapes markup, so we
        # MUST pass style separately rather than embedding [bracket] tags
        # inside the string. The old `[dim]—[/dim]` shape rendered as
        # literal text in the panel.
        if regime.vix is None:
            vix_text, vix_style = "—", "dim"
        else:
            if regime.vix < settings.MACRO_VIX_CALM:
                vix_style = "bright_green"
            elif regime.vix < settings.MACRO_VIX_ELEVATED:
                vix_style = "green"
            elif regime.vix < settings.MACRO_VIX_CRISIS:
                vix_style = "orange1"
            else:
                vix_style = "red blink"
            vix_text = f"{regime.vix:.1f}"

        body = Text()
        body.append("Scenario: ", style="bold")
        body.append(f"{regime.scenario.name}\n", style=scenario_colour)

        score_col = _pnl_colour(regime.macro_score)
        body.append("Score: ", style="bold")
        body.append(f"{regime.macro_score:+.1f}  ",
                    style=f"bold {score_col}")
        body.append_text(self._progress_bar(
            abs(regime.macro_score), 100.0, 10,
        ))
        body.append("\n")

        body.append("DXY: ", style="bold")
        body.append(f"{regime.dxy:.1f}\n"
                    if regime.dxy is not None else "—\n")
        body.append("VIX: ", style="bold")
        body.append(vix_text, style=vix_style)
        body.append("\n")
        body.append("10y-2y: ", style="bold")
        body.append(f"{regime.yield_curve:+.2f}\n"
                    if regime.yield_curve is not None else "—\n")
        body.append("Fed: ", style="bold")
        body.append(f"{regime.fed_funds_rate:.2f}%\n"
                    if regime.fed_funds_rate is not None else "—\n")
        body.append("CPI YoY: ", style="bold")
        body.append(f"{regime.cpi_yoy:.1f}%  "
                    if regime.cpi_yoy is not None else "—  ")
        body.append(f"conf {regime.confidence*100:.0f}%",
                    style="dim")

        return Panel(body, title="[bold]MACRO[/bold]",
                     border_style=scenario_colour)

    def _panel_market(self) -> Panel:
        market = getattr(self._bot, "_market_data", None)
        if market is None:
            return Panel("[dim]Market data not available[/dim]",
                         title="[bold]MARKET OVERVIEW[/bold]", border_style="cyan")

        active = []
        try:
            if hasattr(market, "active_pairs"):
                active = list(market.active_pairs())[:16]
        except Exception:
            pass

        if not active:
            return Panel("[dim]No pairs streaming yet[/dim]",
                         title="[bold]MARKET OVERVIEW[/bold]", border_style="cyan")

        regime_detector = None
        try:
            from core.regime_detector import regime_detector as rd
            regime_detector = rd
        except Exception:
            pass

        t = Table.grid(expand=True)
        for _ in range(4):
            t.add_column(ratio=1)

        cells: list[str] = []
        for pair in active:
            prices = {}
            try:
                if hasattr(market, "get_all_prices"):
                    prices = market.get_all_prices(pair) or {}
            except Exception:
                pass
            if not prices:
                continue
            price = next(iter(prices.values()))
            regime_label = "unknown"
            if regime_detector:
                try:
                    snap = regime_detector.get_primary(pair)
                    if snap:
                        regime_label = snap.regime
                except Exception:
                    pass
            dot = _regime_colour(regime_label)
            cells.append(f"[bold]{pair:10s}[/bold] "
                         f"${price:>9.2f} [{dot}]●[/{dot}]")

        for i in range(0, len(cells), 4):
            row = cells[i:i+4]
            while len(row) < 4:
                row.append("")
            t.add_row(*row)

        return Panel(t, title="[bold]MARKET OVERVIEW[/bold]", border_style="cyan")

    def _panel_agents(self) -> Panel:
        t = Table(expand=True, show_header=True, header_style="bold")
        t.add_column("Agent")
        t.add_column("Status")
        t.add_column("Capital",   justify="right")
        t.add_column("Today P&L", justify="right")
        t.add_column("Trades",    justify="right")

        # Populated by the async run() loop — see _refresh_coordinator_data.
        agent_stats: list[dict] = list(self._agent_stats_cache)

        if not agent_stats:
            # Coordinator not wired — show the planned roster as OFFLINE
            for name in ("Arb", "Signal", "Sentiment", "Macro", "OnChain"):
                t.add_row(name, "[dim]OFFLINE[/dim]", "[dim]—[/dim]",
                          "[dim]—[/dim]", "[dim]—[/dim]")
            t.add_row("[bold]TOTAL[/bold]", "", "[dim]—[/dim]",
                      "[dim]—[/dim]", "[dim]—[/dim]")
            return Panel(t, title="[bold]AGENTS[/bold]", border_style="dim")

        total_cap = 0.0
        total_pnl = 0.0
        total_n   = 0
        for a in agent_stats:
            st = a.get("status", "OFFLINE")
            st_col = _status_colour(st)
            cap = a.get("capital", 0.0)
            pnl = a.get("pnl_pct", 0.0)
            n   = a.get("trades", 0)
            total_cap += cap
            total_pnl += pnl
            total_n   += n
            pnl_col = _pnl_colour(pnl)
            t.add_row(
                a.get("name", "?"),
                f"[{st_col}]{st}[/{st_col}]",
                f"${cap:,.0f}",
                f"[{pnl_col}]{pnl:+.2f}%[/{pnl_col}]",
                str(n),
            )
        total_pnl_col = _pnl_colour(total_pnl)
        t.add_row(
            "[bold]TOTAL[/bold]", "",
            f"[bold]${total_cap:,.0f}[/bold]",
            f"[bold {total_pnl_col}]{total_pnl:+.2f}%[/bold {total_pnl_col}]",
            f"[bold]{total_n}[/bold]",
        )
        return Panel(t, title="[bold]AGENTS[/bold]", border_style="cyan")

    def _panel_circuit_breakers(self) -> Panel:
        cb = getattr(self._bot, "_cb_state", None)
        if cb is None:
            return Panel("[dim]No circuit-breaker state[/dim]",
                         title="[bold]CIRCUIT BREAKERS[/bold]", border_style="cyan")

        daily_loss_limit = settings.CIRCUIT_BREAKERS["daily_loss"]["threshold_pct"]
        consec_limit     = settings.CIRCUIT_BREAKERS["consecutive_loss"]["count"]
        drawdown_limit   = settings.CIRCUIT_BREAKERS["drawdown"]["threshold_pct"]

        body = Text()
        body.append("Daily Loss\n", style="bold")
        body.append_text(self._progress_bar(
            -cb.daily_pnl_pct if cb.daily_pnl_pct < 0 else 0.0,
            daily_loss_limit, 24,
        ))
        body.append(f"  {cb.daily_pnl_pct:.2f}% / -{daily_loss_limit:.1f}%\n\n")

        body.append("Consecutive Losses\n", style="bold")
        body.append_text(self._progress_bar(
            float(cb.consecutive_losses), float(consec_limit), 24,
        ))
        body.append(f"  {cb.consecutive_losses} / {consec_limit}\n\n")

        body.append("Drawdown\n", style="bold")
        dd = abs(cb.drawdown_pct) if cb.drawdown_pct < 0 else 0.0
        body.append_text(self._progress_bar(dd, drawdown_limit, 24))
        body.append(f"  {cb.drawdown_pct:.2f}% / -{drawdown_limit:.1f}%")

        return Panel(body, title="[bold]CIRCUIT BREAKERS[/bold]", border_style="cyan")

    def _panel_top_performers(self) -> Panel:
        recent = []
        try:
            from database import queries as q
            recent = q.get_recent_closed_trades(limit=50)
        except Exception:
            pass

        if not recent or len(recent) < 10:
            return Panel("[dim]Collecting data...[/dim]",
                         title="[bold]TOP PERFORMERS[/bold]", border_style="cyan")

        pair_stats: dict[str, dict] = defaultdict(lambda: {"wins": 0, "total": 0})
        for tr in recent:
            if tr.pnl_pct is None:
                continue
            ps = pair_stats[tr.pair]
            ps["total"] += 1
            if tr.pnl_pct > 0:
                ps["wins"] += 1

        ranked = sorted(
            ((p, s["wins"] / s["total"], s["total"])
             for p, s in pair_stats.items() if s["total"] > 0),
            key=lambda x: x[1], reverse=True,
        )
        body = Text()
        body.append("Top pairs (7d):\n", style="bold")
        for pair, wr, n in ranked[:3]:
            body.append(f"  {pair:12s} {wr*100:.0f}%  ({n})\n", style="green")
        if ranked:
            worst_pair, worst_wr, worst_n = ranked[-1]
            body.append(f"\nWorst: {worst_pair} {worst_wr*100:.0f}% ({worst_n})\n",
                        style="red")
        return Panel(body, title="[bold]TOP PERFORMERS[/bold]", border_style="cyan")

    def _panel_pending_events(self) -> Panel:
        """Next 3 upcoming calendar events from macro_monitor.

        Border colour escalates on proximity to a HIGH-impact event:
        red blinking within 15 min, yellow within 30 min, otherwise cyan.
        """
        events: list = []
        try:
            from macro import macro_monitor
            events = macro_monitor.get_pending_events(n=3)
        except Exception:
            pass

        if not events:
            body = Text("No upcoming events", style="dim italic")
            return Panel(body, title="[bold]PENDING EVENTS[/bold]",
                         border_style="dim")

        # Border escalates only on HIGH-impact proximity.
        border = "cyan"
        soonest_high = None
        for e in events:
            if e.impact.name == "HIGH":
                soonest_high = (
                    e if soonest_high is None
                    else min(soonest_high, e, key=lambda x: x.minutes_until)
                )
        if soonest_high is not None and 0 <= soonest_high.minutes_until:
            if soonest_high.minutes_until <= 15:
                border = "red blink"
            elif soonest_high.minutes_until <= 30:
                border = "yellow"

        impact_style = {"HIGH": "red bold", "MEDIUM": "yellow", "LOW": "white"}
        country_flag = {
            "US": "🇺🇸", "EU": "🇪🇺", "UK": "🇬🇧",
            "JP": "🇯🇵", "CN": "🇨🇳", "CA": "🇨🇦",
        }

        body = Text()
        for ev in events:
            flag = country_flag.get(ev.country, ev.country or "  ")
            mins = ev.minutes_until
            if mins < 60:
                eta = f"{mins:>3d}m"
            elif mins < 60 * 24:
                eta = f"{mins // 60:>3d}h"
            else:
                eta = f"{mins // (60*24):>3d}d"
            style = impact_style.get(ev.impact.name, "white")
            body.append(f"{flag} ", style="white")
            body.append(f"{ev.impact.name[:3]:>3s} ", style=style)
            body.append(f"{eta:>4s}  ", style="dim")
            body.append(f"{ev.title[:32]}\n")
        return Panel(body, title="[bold]PENDING EVENTS[/bold]",
                     border_style=border)

    def _panel_positions(self) -> Panel:
        open_trades = []
        try:
            from database import queries as q
            open_trades = q.get_open_trades()
        except Exception:
            pass

        t = Table(expand=True, show_header=True, header_style="bold")
        t.add_column("Symbol")
        t.add_column("Side")
        t.add_column("Entry",   justify="right")
        t.add_column("Current", justify="right")
        t.add_column("P&L%",    justify="right")
        t.add_column("SL",      justify="right")
        t.add_column("TP",      justify="right")
        t.add_column("Exchange")
        t.add_column("Agent")
        t.add_column("Duration")

        if not open_trades:
            t.add_row("[dim]No open positions[/dim]",
                      "", "", "", "", "", "", "", "", "")
            return Panel(t, title="[bold]POSITIONS[/bold]", border_style="cyan")

        market = getattr(self._bot, "_market_data", None)
        for tr in open_trades:
            current = None
            if market and hasattr(market, "get_price"):
                try:
                    current = market.get_price(tr.exchange, tr.pair)
                except Exception:
                    pass
            if current is None:
                current = tr.entry_price or 0.0

            if tr.entry_price:
                if tr.side == "long":
                    pnl_pct = (current - tr.entry_price) / tr.entry_price * 100
                else:
                    pnl_pct = (tr.entry_price - current) / tr.entry_price * 100
            else:
                pnl_pct = 0.0

            side_col = "green" if tr.side == "long" else "red"
            pnl_col = _pnl_colour(pnl_pct)
            dur = ""
            if tr.timestamp_open:
                dur = self._fmt_duration(datetime.utcnow() - tr.timestamp_open)

            t.add_row(
                tr.pair,
                f"[{side_col}]{(tr.side or '?').upper()}[/{side_col}]",
                f"{tr.entry_price:.4f}" if tr.entry_price else "—",
                f"{current:.4f}"        if current else "—",
                f"[{pnl_col}]{pnl_pct:+.2f}%[/{pnl_col}]",
                f"{tr.stop_loss:.4f}"   if tr.stop_loss   else "—",
                f"{tr.take_profit:.4f}" if tr.take_profit else "—",
                tr.exchange    or "—",
                tr.signal_type or "—",
                dur or "—",
            )
        return Panel(t, title="[bold]POSITIONS[/bold]", border_style="cyan")

    def _panel_signal_feed(self) -> Panel:
        t = Table(expand=True, show_header=True, header_style="bold")
        t.add_column("Time")
        t.add_column("Symbol")
        t.add_column("Track")
        t.add_column("Score", justify="right")
        t.add_column("Regime")
        t.add_column("Agent")
        t.add_column("Action")

        if not self._signal_buffer:
            t.add_row("[dim]—[/dim]", "", "", "", "", "", "")
            return Panel(t, title="[bold]SIGNAL FEED[/bold]", border_style="cyan")

        for s in list(self._signal_buffer)[-15:][::-1]:
            action = s["action"]
            colour = {"EXEC": "bright_green", "SKIP": "dim",
                      "PENDING": "yellow"}.get(action, "white")
            rcol = _regime_colour(s["regime"])
            t.add_row(
                s["time"], s["symbol"], s["track"],
                f"{s['score']:.0f}",
                f"[{rcol}]{s['regime']}[/{rcol}]",
                s["agent"] or "—",
                f"[{colour}]{action}[/{colour}]",
            )
        return Panel(t, title="[bold]SIGNAL FEED[/bold]", border_style="cyan")

    def _panel_arb_feed(self) -> Panel:
        t = Table(expand=True, show_header=True, header_style="bold")
        t.add_column("Time")
        t.add_column("Pair")
        t.add_column("Buy Ex")
        t.add_column("Sell Ex")
        t.add_column("Gap%",    justify="right")
        t.add_column("Net P&L", justify="right")

        if not self._arb_buffer:
            t.add_row("[dim]—[/dim]", "", "", "", "", "")
        else:
            for a in list(self._arb_buffer)[-10:][::-1]:
                pnl_col = _pnl_colour(a["pnl"])
                t.add_row(
                    a["time"], a["pair"], a["buy"], a["sell"],
                    f"{a['gap_pct']:.3f}",
                    f"[{pnl_col}]{a['pnl']:+.4f}[/{pnl_col}]",
                )

        # Capital-gate counter sits inline under the feed so the
        # operator sees blocked executions next to fired ones. Resilient
        # to older ArbEngine instances that don't carry the attribute —
        # TODO: hook into the engine's daily reset once a reset hook is
        # exposed (today the counter survives until process restart).
        missed = int(self._arb_engine_stats_cache.get("missed_balance_checks", 0) or 0)
        if missed >= settings.DASHBOARD_BALANCE_MISS_RED:
            miss_style  = "red bold"
            miss_suffix = f"  ({missed} blocked today)"
        elif missed >= settings.DASHBOARD_BALANCE_MISS_AMBER:
            miss_style  = "yellow"
            miss_suffix = f"  ({missed} blocked today)"
        else:
            miss_style  = "dim"
            miss_suffix = ""
        footer = Text()
        footer.append("Missed (balance): ", style="bold")
        footer.append(str(missed), style=miss_style)
        footer.append(miss_suffix, style=miss_style)

        return Panel(self._stack(t, footer), title="[bold]ARB FEED[/bold]",
                     border_style="cyan")

    def _panel_arb_opportunities(self) -> Panel:
        """Detection-vs-execution funnel for the arb engine.

        Reads from queries.get_arb_opportunity_stats — failure or empty
        stats render a placeholder rather than crashing the panel.
        """
        title = "[bold]ARB OPPORTUNITIES[/bold]"
        try:
            from database import queries as q
            stats = q.get_arb_opportunity_stats() or {}
        except Exception as e:
            logger.debug(f"arb opportunity stats query failed: {e}")
            return Panel(
                Text("No opportunities detected yet", style="dim italic"),
                title=title, border_style="dim",
            )

        total      = int(stats.get("total_detected", 0) or 0)
        if total == 0:
            return Panel(
                Text("No opportunities detected yet", style="dim italic"),
                title=title, border_style="dim",
            )

        executed   = int(stats.get("total_executed", 0) or 0)
        exec_rate  = float(stats.get("execution_rate_pct", 0.0) or 0.0)
        avg_gap    = float(stats.get("avg_gap_pct", 0.0) or 0.0)
        max_gap    = float(stats.get("max_gap_pct", 0.0) or 0.0)
        top_pairs  = list(stats.get("top_pairs") or [])

        # above_threshold isn't carried directly in the stats dict —
        # derive it from execution_rate when both sides are known.
        # execution_rate_pct = executed / above_threshold × 100, so
        # above_threshold = executed × 100 / exec_rate (when nonzero).
        above_threshold: int
        if exec_rate > 0 and executed > 0:
            above_threshold = int(round(executed * 100.0 / exec_rate))
        else:
            above_threshold = executed

        # Colour ladder for the executed line — green when we're
        # catching the majority, amber mid-range, red when most viable
        # gaps slip past.
        if exec_rate >= settings.DASHBOARD_EXEC_RATE_GREEN_PCT:
            exec_style = "bright_green"
        elif exec_rate >= settings.DASHBOARD_EXEC_RATE_AMBER_PCT:
            exec_style = "yellow"
        else:
            exec_style = "red"

        body = Text()
        body.append(f"Detected today:  {total:>6d}\n", style="dim")
        body.append(f"Above threshold: {above_threshold:>6d}\n", style="white")
        body.append("Executed:        ", style="bold")
        body.append(f"{executed:>6d}", style=exec_style)
        body.append(f"  ({exec_rate:.0f}% of above)\n", style=exec_style)
        body.append(f"Avg gap:         {avg_gap:.3f}%\n", style="white")
        body.append(f"Max gap:         {max_gap:.3f}%\n", style="white")
        if top_pairs:
            pair, count = top_pairs[0]
            body.append("Top pair:        ", style="bold")
            body.append(f"{pair}", style="cyan")
            body.append(f" ({count} detections)", style="dim")

        return Panel(body, title=title, border_style="cyan")

    def _panel_exchange_health(self) -> Panel:
        t = Table(expand=True, show_header=True, header_style="bold")
        t.add_column("Exchange")
        t.add_column("Status")
        t.add_column("Latency",   justify="right")
        t.add_column("Last Feed")

        exchanges = list(self._exchange_health.keys()) or list(settings.ENABLED_EXCHANGES)
        for ex in exchanges:
            h = self._exchange_health.get(ex)
            if h is None:
                t.add_row(ex, "[dim]—[/dim]", "[dim]—[/dim]", "[dim]—[/dim]")
                continue
            status = "[green]✓[/green]" if h.connected else "[red]✗[/red]"
            lat_col = ("green"  if h.latency_ms < 100 else
                       "yellow" if h.latency_ms < 500 else "red")
            last = h.last_seen.strftime("%H:%M:%S") if h.last_seen else "—"
            t.add_row(
                ex, status,
                f"[{lat_col}]{h.latency_ms:.0f}ms[/{lat_col}]",
                last,
            )
        return Panel(t, title="[bold]EXCHANGES[/bold]", border_style="cyan")

    def _panel_session_perf(self) -> Panel:
        sessions = ("LONDON", "NEW_YORK", "ASIA", "OFF_HOURS")
        t = Table(expand=True, show_header=True, header_style="bold")
        t.add_column("Session")
        t.add_column("Trades", justify="right")
        t.add_column("P&L %",  justify="right")

        trades = []
        try:
            from database import queries as q
            trades = q.get_today_trades()
        except Exception:
            pass

        agg = defaultdict(lambda: {"n": 0, "pnl": 0.0})
        for tr in trades:
            sess = self._session_for(tr.timestamp_open)
            agg[sess]["n"] += 1
            if tr.pnl_pct is not None:
                agg[sess]["pnl"] += tr.pnl_pct * 100

        for sess in sessions:
            s = agg.get(sess)
            if s and s["n"] > 0:
                pnl_col = _pnl_colour(s["pnl"])
                t.add_row(sess, str(s["n"]),
                          f"[{pnl_col}]{s['pnl']:+.2f}%[/{pnl_col}]")
            else:
                t.add_row(sess, "[dim]—[/dim]", "[dim]—[/dim]")
        return Panel(t, title="[bold]SESSION PERFORMANCE[/bold]", border_style="cyan")

    def _panel_log_feed(self) -> Panel:
        t = Table(expand=True, show_header=False)
        t.add_column("time",  width=10)
        t.add_column("level", width=10)
        t.add_column("msg")

        level_colour = {
            "INFO":     "white",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "red bold",
            "TRADE":    "bright_green",
            "SIGNAL":   "cyan",
            "ARB":      "magenta",
            "SYSTEM":   "dim white",
        }

        if not self._log_buffer:
            t.add_row("[dim]—[/dim]", "", "")
            return Panel(t, title="[bold]LOG[/bold]", border_style="cyan")

        for entry in list(self._log_buffer)[-18:]:
            lvl = entry["level"]
            c = level_colour.get(lvl, "white")
            t.add_row(entry["time"], f"[{c}]{lvl}[/{c}]", entry["msg"][:70])
        return Panel(t, title="[bold]LOG[/bold]", border_style="cyan")

    def _panel_approval(self) -> Panel:
        pending = self._peek_pending()
        if not pending:
            # Idle — surface the keystroke vocabulary so the user knows
            # what to type once a signal arrives. ApprovalInputHandler
            # (ui/prompts.py) is the only consumer of these keys.
            body = Text()
            body.append("Waiting for signals…\n\n",
                        style="dim italic")
            body.append("Commands: ", style="bold")
            body.append("a=approve  s=skip  k=kill  q=quit",
                        style="cyan")
            return Panel(body, title="[bold]APPROVAL[/bold]",
                         border_style="dim")

        body = Text()
        body.append("PENDING: ", style="bold yellow")
        body.append(f"{pending.get('pair','?')}  ", style="bold")
        direction = pending.get("direction", "?")
        body.append(f"{direction.upper()}  ",
                    style="green" if direction == "long" else "red")
        body.append(f"score={pending.get('score',0):.0f}\n")
        body.append(f"Track: {pending.get('track','?')}  "
                    f"Regime: {pending.get('regime','?')}\n")
        body.append(f"Entry: {pending.get('entry','—')}  "
                    f"SL: {pending.get('sl','—')}  "
                    f"TP: {pending.get('tp','—')}  "
                    f"R/R: {pending.get('rr','—')}\n\n")
        reasoning = (pending.get("reasoning") or "")[:200]
        if reasoning:
            body.append(f"{reasoning}\n\n", style="italic")
        # Single-key vocabulary matches ui.prompts.ApprovalInputHandler.
        # Keep the legacy [G]/[S]/[M]/[I]/[K] hints out — they were
        # decorative and confused the user about what actually worked.
        body.append("a=approve  s=skip  k=kill  q=quit",
                    style="bold yellow")
        return Panel(body, title="[bold yellow]APPROVAL PENDING[/bold yellow]",
                     border_style="yellow")

    def _panel_insights(self) -> Panel:
        # Seed from DB if push buffer is empty
        if not self._insight_buffer:
            try:
                from database import queries as q
                for tr in q.get_recent_closed_trades(limit=10):
                    if getattr(tr, "claude_postmortem", None):
                        first_line = tr.claude_postmortem.splitlines()[0]
                        self._insight_buffer.append(first_line)
                        if len(self._insight_buffer) >= self.INSIGHT_BUFFER_MAX:
                            break
            except Exception:
                pass

        if not self._insight_buffer:
            return Panel(
                Text("No insights yet — collecting data", style="dim italic"),
                title="[bold]🧠 INSIGHTS[/bold]", border_style="dim",
            )

        width = max(40, self._console.width - 14)
        body = Text()
        for line in list(self._insight_buffer):
            body.append(f"🧠 {line[:width]}\n", style="cyan")
        return Panel(body, title="[bold]🧠 INSIGHTS[/bold]", border_style="cyan")

    def _panel_footer(self) -> Panel:
        t = Table.grid(expand=True)
        t.add_column(justify="center")
        t.add_row("[bold]Controls:[/bold] "
                  "[yellow][K][/yellow] Kill all  "
                  "[yellow][W][/yellow] Approve window  "
                  "[yellow][P][/yellow] Pause  "
                  "[yellow][Q][/yellow] Quit")
        t.add_row("[bold]Approval:[/bold] "
                  "[green][G][/green] Go  "
                  "[red][S][/red] Skip  "
                  "[cyan][M][/cyan] Modify  "
                  "[blue][I][/blue] Info  "
                  "[magenta][1-5][/magenta] Select agent")
        return Panel(t, border_style="dim")

    def _panel_cmd_bar(self) -> Panel:
        """Command bar — bottom-row panel that echoes the most recent
        input line and shows the last completed command's outcome.

        The actual prompt prints to the terminal below the dashboard
        (Live runs with screen=False so stdin echo is visible). The
        ApprovalInputHandler thread writes to bot._cmd_current /
        _cmd_last_result / _cmd_last_ts on every line — this panel
        just reads those.
        """
        current = getattr(self._bot, "_cmd_current",     "") or ""
        last    = getattr(self._bot, "_cmd_last_result", "") or ""
        ts      = getattr(self._bot, "_cmd_last_ts",     None)

        prompt = current if current else "Type command below ↓"
        prompt_style = "bold green" if current else "dim italic"

        last_line = Text()
        if last:
            last_line.append("Last: ", style="bold")
            last_line.append(last, style="white")
            if ts:
                last_line.append(f"  [{ts}]", style="dim")
            last_line.append("    ")
        last_line.append(
            "a=approve  s=skip  w=window  k=kill  p=pause  q=quit  h=help",
            style="cyan",
        )

        body = Table.grid(expand=True)
        body.add_column(justify="left")
        body.add_row(Text("CMD  > ", style="bold yellow") + Text(prompt, style=prompt_style))
        body.add_row(Text("      ", style="dim") + last_line)
        return Panel(body, border_style="yellow", title="[bold yellow]COMMAND[/bold yellow]")

    # ════════════════════════════════════════════════════════════════════
    # Helpers
    # ════════════════════════════════════════════════════════════════════

    def _stack(self, *items):
        """Vertically stack mixed Text + Table content into a Rich group."""
        from rich.console import Group
        return Group(*items)

    def _sentiment_latest(self) -> dict:
        sent = getattr(self._bot, "_sentiment", None)
        if sent is None:
            return {}
        return getattr(sent, "latest", {}) or {}

    def _read_trade_stats(self):
        """(trades_today, wins, losses, win_rate_today, win_rate_all)."""
        try:
            from database import queries as q
            today = q.get_today_trades()
            wr_today = q.get_signal_win_rate(days=1)
            wr_all   = q.get_signal_win_rate(days=365)
        except Exception:
            return 0, 0, 0, None, None
        closed = [t for t in today if t.pnl_pct is not None]
        wins = sum(1 for t in closed if t.pnl_pct > 0)
        losses = sum(1 for t in closed if t.pnl_pct <= 0)
        return (
            len(today), wins, losses,
            wr_today.get("win_rate") if wr_today.get("total") else None,
            wr_all.get("win_rate")   if wr_all.get("total")   else None,
        )

    def _open_exposure_pct(self, equity: float) -> float:
        if equity <= 0:
            return 0.0
        try:
            from database import queries as q
            open_trades = q.get_open_trades()
        except Exception:
            return 0.0
        total = sum((t.size_usd or 0.0) for t in open_trades)
        return total / equity * 100

    def _signals_fired_count(self) -> int:
        try:
            from database import queries as q
            return len(q.get_today_trades())
        except Exception:
            return 0

    def _signals_skipped_count(self) -> int:
        try:
            from database import queries as q
            return q.get_today_skipped_signals()
        except Exception:
            return 0

    def _last_signal_time(self) -> str:
        if self._signal_buffer:
            return list(self._signal_buffer)[-1]["time"]
        return "—"

    def _peek_pending(self) -> Optional[dict]:
        """Render-friendly view of the next pending signal, or None if empty.

        Calls bot.peek_pending() — the bot owns the queue-internal access.
        Falls back to None on missing method (e.g. test stub) or any error.
        """
        peek = getattr(self._bot, "peek_pending", None)
        if not callable(peek):
            return None
        try:
            sig = peek()
        except Exception:
            return None
        if sig is None:
            return None
        return {
            "pair":      getattr(sig, "pair", "?"),
            "direction": getattr(sig, "direction", "?"),
            "score":     float(getattr(sig, "score", 0) or 0),
            "track":     getattr(sig, "signal_type", "?"),
            "regime":    "?",
            "entry":     getattr(sig, "suggested_entry", "—"),
            "sl":        getattr(sig, "suggested_sl", "—"),
            "tp":        getattr(sig, "suggested_tp", "—"),
            "rr":        getattr(sig, "risk_reward", "—"),
            "reasoning": getattr(sig, "claude_reasoning", "") or "",
        }

    def _current_session(self, utc: datetime) -> str:
        h = utc.hour
        if 7 <= h < 13:
            return "LONDON"
        if 13 <= h < 20:
            return "NEW_YORK"
        if 0 <= h < 7:
            return "ASIA"
        return "OFF_HOURS"

    def _session_for(self, dt: Optional[datetime]) -> str:
        if dt is None:
            return "OFF_HOURS"
        return self._current_session(dt)

    def _fmt_duration(self, delta: timedelta) -> str:
        total = int(delta.total_seconds())
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        if h:
            return f"{h}h{m:02d}m"
        if m:
            return f"{m}m{s:02d}s"
        return f"{s}s"

    def _progress_bar(self, value: float, total: float, width: int) -> Text:
        """Coloured text-based bar that shifts green→yellow→red as it fills."""
        if total <= 0:
            return Text("░" * width, style="dim")
        ratio = max(0.0, min(1.0, value / total))
        fill = int(ratio * width)
        empty = width - fill
        if ratio < 0.5:
            colour = "green"
        elif ratio < 0.8:
            colour = "yellow"
        else:
            colour = "red"
        bar = Text()
        bar.append("█" * fill, style=colour)
        bar.append("░" * empty, style="dim")
        return bar
