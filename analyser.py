"""Deterministic trade-brief generator. No AI calls.

Consumes the score/direction/signals dict from `smc_detector.score_setups`
and produces a structured brief with mathematically-derived entry zone,
stop loss, take profits and R:R.

Two setups in play:
  - Order Block Retest (signals["ob_retest"])
  - Break of Structure Retest (signals["bos_retest"])

Score thresholds:
  - 100 (both aligned)  → take_trade=True
  -  50 (one only)      → take_trade=False (below ANALYSIS_MIN_SCORE=75)
  -   0 (none/conflict) → take_trade=False
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from config import (
    ANALYSIS_MIN_SCORE,
    COOLDOWN_DAYS,
    DISPLAY_MIN_SCORE,
    GEOPOLITICAL_ASSETS,
    SL_ATR_MULT,
    SL_BUFFER_PCT,
    TRADING_WINDOWS,
)
import cooling_off
from enricher import hours_until_next_high_impact
from memory import compute_win_rate


def _zone_from_signals(direction: str, signals: dict[str, Any]) -> tuple[float, float] | None:
    """Pick the entry zone. Prefer the OB zone (it's a real range); fall back
    to a thin band around the BOS level."""
    ob = signals.get("ob_retest")
    if ob:
        return float(ob["ob_low"]), float(ob["ob_high"])
    bos = signals.get("bos_retest")
    if bos:
        level = float(bos["level"])
        # 0.15% half-width band around the level — tight enough to be precise,
        # wide enough that a daily-bar fill is plausible.
        half = level * 0.0015
        if direction == "long":
            return level - half, level + half
        else:
            return level - half, level + half
    return None


def compute_levels(
    direction: str,
    signals: dict[str, Any],
    atr14: float | None = None,
) -> dict[str, Any] | None:
    """Compute entry zone, SL, TP1, TP2, R:R from the active signals.

    SL placement is range-aware: SL = OB extreme ± max(0.3% × price, 0.5 × ATR14).
    Falls back to the fixed 0.3% buffer if ATR is unavailable.
    """
    zone = _zone_from_signals(direction, signals)
    if zone is None:
        return None
    zone_low, zone_high = zone
    if zone_low >= zone_high:
        return None
    entry = round((zone_low + zone_high) / 2, 5)

    atr_buf = SL_ATR_MULT * atr14 if (atr14 is not None and atr14 > 0) else 0.0

    if direction == "long":
        pct_buf = zone_low * SL_BUFFER_PCT
        buf = max(pct_buf, atr_buf)
        sl = round(zone_low - buf, 5)
        if sl >= entry:
            return None
        risk = entry - sl
        tp1 = round(entry + 2 * risk, 5)
        tp2 = round(entry + 3 * risk, 5)
    else:
        pct_buf = zone_high * SL_BUFFER_PCT
        buf = max(pct_buf, atr_buf)
        sl = round(zone_high + buf, 5)
        if sl <= entry:
            return None
        risk = sl - entry
        tp1 = round(entry - 2 * risk, 5)
        tp2 = round(entry - 3 * risk, 5)

    if risk <= 0:
        return None

    return {
        "entry": entry,
        "entry_zone_low": round(zone_low, 5),
        "entry_zone_high": round(zone_high, 5),
        "stop_loss": sl,
        "take_profit_1": tp1,
        "take_profit_2": tp2,
        "rr_ratio": "1:2.00",
        "sl_buffer_used": round(buf, 5),
        "sl_buffer_source": "atr" if atr_buf > pct_buf else "pct",
    }


def _patterns_detected(signals: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if signals.get("ob_retest"):
        ob = signals["ob_retest"]
        out.append(f"{ob['direction']} OB retest ({ob['impulse_pct'] * 100:.1f}% impulse)")
    if signals.get("bos_retest"):
        bos = signals["bos_retest"]
        out.append(f"{bos['direction']} BOS retest @ {bos['level']}")
    return out


def _warnings(symbol: str, direction: str | None, enrichment: dict[str, Any]) -> list[str]:
    warns: list[str] = []
    if symbol in GEOPOLITICAL_ASSETS:
        warns.append("Active geopolitical risk on this asset — review headlines before entry.")
    events = enrichment.get("upcoming_events", [])
    hrs = hours_until_next_high_impact(events)
    if hrs is not None and hrs <= 4:
        next_e = min((e for e in events if e.get("impact") in ("high", "very high")),
                     key=lambda e: e["time"])
        warns.append(f"{next_e['event']} in {hrs:.1f}h — consider waiting for the dust to settle.")
    return warns


def _confidence_bucket(score: int) -> str:
    if score >= 100:
        return "high"
    if score >= 75:
        return "medium"
    return "low"


def _build_reasoning(symbol: str, score: int, direction: str | None,
                     signals: dict[str, Any], levels: dict[str, Any] | None,
                     news_sentiment: dict[str, Any] | None = None,
                     macro_warning: str | None = None,
                     staleness_reasons: list[str] | None = None,
                     regime_block: str | None = None) -> str:
    parts: list[str] = []
    ob = signals.get("ob_retest")
    bos = signals.get("bos_retest")

    if regime_block:
        parts.append(f"{symbol}: {regime_block}.")

    if staleness_reasons:
        parts.append(
            f"{symbol}: setup invalidated by intraday move — "
            + "; ".join(staleness_reasons) + "."
        )

    if ob and bos and ob["direction"] == bos["direction"]:
        parts.append(
            f"Dual confluence — {ob['direction']} OB retest stacked with "
            f"a {bos['direction']} BOS retest at {bos['level']}."
        )
        parts.append(
            f"Impulse of {ob['impulse_pct'] * 100:.1f}% originated at {ob['ob_low']}-{ob['ob_high']}; "
            f"price has now returned to that zone for the first time."
        )
    elif ob:
        parts.append(
            f"{ob['direction']} OB retest only. "
            f"{ob['impulse_pct'] * 100:.1f}% impulse origin at {ob['ob_low']}-{ob['ob_high']}; "
            "no aligned BOS retest yet."
        )
    elif bos:
        parts.append(
            f"{bos['direction']} BOS retest only at {bos['level']}; no aligned OB retest yet."
        )
    elif not staleness_reasons and not regime_block:
        parts.append(f"{symbol}: no qualifying setup right now.")

    if levels and direction:
        parts.append(
            f"Entry {levels['entry_zone_low']}-{levels['entry_zone_high']}, "
            f"SL {levels['stop_loss']}, TP1 {levels['take_profit_1']}, TP2 {levels['take_profit_2']}, "
            f"R:R {levels['rr_ratio']}."
        )

    if news_sentiment:
        sent = news_sentiment.get("sentiment", "neutral")
        net = news_sentiment.get("score", 0)
        count = news_sentiment.get("headline_count", 0)
        if sent == "neutral":
            parts.append(f"News: neutral across {count} headlines (net {net:+d}).")
        else:
            aligned = direction is not None and (
                (sent == "bullish" and direction == "long")
                or (sent == "bearish" and direction == "short")
            )
            tag = "aligned" if aligned else "conflicting" if direction else "no direction"
            parts.append(
                f"News: {sent} (net {net:+d} across {count} headlines, {tag} with technicals)."
            )

    if macro_warning:
        parts.append(macro_warning)

    parts.append(f"Score {score}/100.")
    if score < ANALYSIS_MIN_SCORE:
        parts.append(f"Below the {ANALYSIS_MIN_SCORE} threshold — no trade.")

    text = " ".join(parts)
    words = text.split()
    if len(words) > 150:
        text = " ".join(words[:150]) + "…"
    return text


def build_brief(
    symbol: str,
    score: int,
    direction: str | None,
    signals: dict[str, Any],
    current_price: float | None,
    bias: str,
    atr14: float | None = None,
    enrichment: dict[str, Any] | None = None,
    intraday: dict[str, Any] | None = None,
    news_sentiment: dict[str, Any] | None = None,
    news_warnings: list[str] | None = None,
    macro_warning: str | None = None,
    staleness_reasons: list[str] | None = None,
    skip_cooling_off: bool = False,
) -> dict[str, Any]:
    enrichment = enrichment or {"headlines": [], "upcoming_events": []}
    headlines = enrichment.get("headlines", [])
    news_warnings = list(news_warnings or [])
    staleness_reasons = list(staleness_reasons or [])
    regime_block = (signals or {}).get("regime_blocked")

    # Cooling-off check — symbols with poor recent track records can't fire
    # alerts even if the technical confluence is high. Backtest disables this
    # via skip_cooling_off so the simulation isn't gated by the live blacklist.
    cooling_off_entry = None if skip_cooling_off else cooling_off.is_cooling_off(symbol)

    levels = compute_levels(direction, signals, atr14=atr14) if direction else None

    take_trade = (
        score >= ANALYSIS_MIN_SCORE
        and direction is not None
        and levels is not None
        and cooling_off_entry is None
    )

    confidence = _confidence_bucket(score)
    if macro_warning and confidence == "high":
        confidence = "medium"

    # Rolling win rate from the live trade log. Returns None until at least
    # one outcome has been logged via the dashboard.
    rate_n = compute_win_rate(symbol)
    win_rate = rate_n[0] if rate_n else None
    win_rate_n = rate_n[1] if rate_n else 0

    warnings = _warnings(symbol, direction, enrichment)
    # Front-load reasons that explain a collapsed/zero score: cooling-off
    # is the strongest gate (suppresses regardless of score), then regime
    # block, then staleness, then everything else.
    if cooling_off_entry:
        wins = cooling_off_entry.get("wins", 0)
        n = cooling_off_entry.get("n", 0)
        warnings.insert(0,
            f"cooling off ({wins}/{n} in last {COOLDOWN_DAYS} days) — "
            f"signals suppressed until {cooling_off_entry.get('until', '?')[:10]}"
        )
    if regime_block:
        warnings.insert(0 if not cooling_off_entry else 1, regime_block)
    warnings = staleness_reasons + warnings
    warnings.extend(news_warnings)
    if macro_warning:
        warnings.append(macro_warning)

    brief: dict[str, Any] = {
        "take_trade": take_trade,
        "symbol": symbol,
        "direction": direction,
        "current_price": current_price,
        "confluence_score": score,
        "confidence": confidence,
        "htf_bias": bias,
        "atr14": round(atr14, 5) if atr14 is not None else None,
        "patterns_detected": _patterns_detected(signals),
        "signals": signals,
        "intraday": intraday,
        "headlines": headlines,
        "news_sentiment": (news_sentiment or {}).get("sentiment"),
        "news_score": (news_sentiment or {}).get("score"),
        "news_top_headlines": (news_sentiment or {}).get("top_headlines", []),
        "news_headline_count": (news_sentiment or {}).get("headline_count", 0),
        "macro_warning": macro_warning,
        "cooling_off": cooling_off_entry,
        "upcoming_events": enrichment.get("upcoming_events", []),
        "warnings": warnings,
        "best_window": TRADING_WINDOWS.get(symbol, "13:30-16:00 GMT (US open + first 2.5h)"),
        "historical_win_rate_10d": win_rate,
        "win_rate_sample_size": win_rate_n,
        "reasoning": "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    if levels:
        brief.update(levels)
    else:
        brief.update({
            "entry": None, "entry_zone_low": None, "entry_zone_high": None,
            "stop_loss": None, "take_profit_1": None, "take_profit_2": None,
            "rr_ratio": None, "sl_buffer_used": None, "sl_buffer_source": None,
        })

    brief["reasoning"] = _build_reasoning(
        symbol, score, direction, signals, levels,
        news_sentiment=news_sentiment, macro_warning=macro_warning,
        staleness_reasons=staleness_reasons,
        regime_block=regime_block,
    )
    return brief


# ---------- Terminal rendering ----------

def _fmt(n: float | int | None) -> str:
    if n is None:
        return "—"
    if isinstance(n, float):
        return f"{n:,.5f}".rstrip("0").rstrip(".") or "0"
    return str(n)


def _intraday_summary(intraday: dict[str, Any] | None) -> str | None:
    """One-line summary of intraday detector status. None if no data."""
    if not intraday:
        return None
    parts: list[str] = []
    h4 = intraday.get("h4")
    h1 = intraday.get("h1")
    if h4:
        d = (h4.get("direction") or "—").upper()
        parts.append(f"4H {h4.get('score', 0)}/{d}")
    else:
        parts.append("4H —")
    if h1:
        d = (h1.get("direction") or "—").upper()
        parts.append(f"1H {h1.get('score', 0)}/{d}")
    else:
        parts.append("1H —")
    parts.append(f"({intraday.get('h1_bars', 0)} 1H bars)")
    return "  ·  ".join(parts)


def _news_badge(brief: dict[str, Any]) -> str | None:
    """Compact one-line badge: `News: BULLISH (+3 / 10 hdl)` or None."""
    sent = brief.get("news_sentiment")
    if not sent:
        return None
    net = brief.get("news_score", 0) or 0
    count = brief.get("news_headline_count", 0) or 0
    return f"News: {sent.upper()} (net {net:+d} / {count} hdl)"


def _top_news_headline(brief: dict[str, Any]) -> str | None:
    top = brief.get("news_top_headlines") or []
    if not top:
        return None
    h = top[0]
    title = (h.get("title") or "").strip()
    if not title:
        return None
    pub = (h.get("publisher") or "").strip()
    score = h.get("score", 0)
    sign = "↑" if score > 0 else "↓" if score < 0 else "·"
    suffix = f" [{pub}]" if pub else ""
    return f"{sign} {title}{suffix}"


def _alignment_note(daily_direction: str | None, intraday: dict[str, Any] | None) -> str | None:
    """If daily and an intraday timeframe agree on direction with score >= 50,
    return a short alignment note; otherwise None."""
    if not daily_direction or not intraday:
        return None
    aligned = []
    for tf in ("h4", "h1"):
        block = intraday.get(tf)
        if block and block.get("direction") == daily_direction and block.get("score", 0) >= 50:
            aligned.append(tf.upper())
    if not aligned:
        return None
    return f"intraday confluence: {', '.join(aligned)} agrees with daily {daily_direction}"


def render_brief(brief: dict[str, Any]) -> str:
    bar = "═" * 60
    out: list[str] = [bar]
    head = f"  {brief['symbol']}"
    if brief.get("direction"):
        head += f" — {brief['direction'].upper()}"
    if not brief.get("take_trade"):
        head += "  [NO TRADE]"
    out.append(head)
    out.append(f"  Confluence: {brief['confluence_score']}/100 ({brief['confidence']})")
    out.append(f"  Bias (60d): {brief['htf_bias']}")
    if brief.get("current_price") is not None:
        out.append(f"  Current price: {_fmt(brief['current_price'])}")
    if brief.get("atr14") is not None:
        out.append(f"  ATR(14):       {_fmt(brief['atr14'])}")
    win_rate = brief.get("historical_win_rate_10d")
    if win_rate is not None:
        out.append(f"  Historical:    {win_rate * 100:.1f}% win-rate (10-day, {brief['symbol']} only)")
    out.append(bar)

    if brief.get("take_trade"):
        out.append(f"  Entry zone:    {_fmt(brief['entry_zone_low'])} – {_fmt(brief['entry_zone_high'])}  (mid {_fmt(brief['entry'])})")
        sl_src = brief.get("sl_buffer_source")
        sl_buf = brief.get("sl_buffer_used")
        sl_note = f"  [{sl_src}-buffer {_fmt(sl_buf)}]" if sl_src else ""
        out.append(f"  Stop loss:     {_fmt(brief['stop_loss'])}{sl_note}")
        out.append(f"  Take profit 1: {_fmt(brief['take_profit_1'])}")
        out.append(f"  Take profit 2: {_fmt(brief['take_profit_2'])}")
        out.append(f"  R:R:           {brief['rr_ratio']}")
        out.append(f"  Best window:   {brief['best_window']}")
    else:
        out.append("  No actionable trade — see reasoning below.")

    intraday_line = _intraday_summary(brief.get("intraday"))
    if intraday_line:
        out.append("")
        out.append(f"  Intraday:    {intraday_line}")
        align = _alignment_note(brief.get("direction"), brief.get("intraday"))
        if align:
            out.append(f"               ✓ {align}")

    news_badge = _news_badge(brief)
    if news_badge:
        out.append("")
        out.append(f"  {news_badge}")
        top = _top_news_headline(brief)
        if top:
            out.append(f"    {top}")

    if brief.get("macro_warning"):
        out.append("")
        out.append(f"  ⚠ {brief['macro_warning']}")

    if brief.get("patterns_detected"):
        out.append("")
        out.append("  Setups detected:")
        for p in brief["patterns_detected"]:
            out.append(f"    • {p}")

    if brief.get("warnings"):
        out.append("")
        out.append("  Warnings:")
        for w in brief["warnings"]:
            out.append(f"    ⚠ {w}")

    if brief.get("headlines"):
        out.append("")
        out.append("  Recent headlines:")
        for h in brief["headlines"]:
            out.append(f"    • [{(h.get('publisher') or '').strip()}] {h.get('title', '').strip()}")

    out.append("")
    out.append("  Reasoning:")
    out.append(f"    {brief['reasoning']}")
    out.append(bar)
    return "\n".join(out)


def render_daily_briefing(results: list[dict[str, Any]]) -> str:
    """Top-3 daily briefing. Symbols are ranked by confluence_score; only the
    top 3 are surfaced. If the top score is below DISPLAY_MIN_SCORE the
    briefing collapses to a single 'no quality setups today' line — the rest
    of the universe is intentionally suppressed to keep attention sharp."""
    bar = "═" * 60
    lines: list[str] = [bar, "  DAILY BRIEFING — top 3", bar]
    if not results:
        lines.append("  No symbols scanned.")
        lines.append(bar)
        return "\n".join(lines)

    sorted_all = sorted(results, key=lambda r: r.get("confluence_score", 0) or 0, reverse=True)
    top_score = sorted_all[0].get("confluence_score", 0) or 0
    if top_score < DISPLAY_MIN_SCORE:
        lines.append("")
        lines.append(f"  No quality setups today — top score {top_score}/100 (need ≥{DISPLAY_MIN_SCORE}).")
        lines.append(f"  Scanned {len(results)} symbols.")
        lines.append("")
        lines.append(bar)
        return "\n".join(lines)

    top3 = sorted_all[:3]
    for rank, r in enumerate(top3, start=1):
        sym = r.get("symbol", "?")
        score = r.get("confluence_score", 0)
        direction = (r.get("direction") or "—").upper()
        win_rate = r.get("historical_win_rate_10d")
        n = r.get("win_rate_sample_size", 0) or 0
        prob = f"{win_rate * 100:.0f}% (n={n})" if win_rate is not None else "n/a"
        price = _fmt(r.get("current_price"))
        atr = _fmt(r.get("atr14"))

        lines.append("")
        intraday_line = _intraday_summary(r.get("intraday"))
        align = _alignment_note(r.get("direction"), r.get("intraday"))
        news_badge = _news_badge(r)
        top_news = _top_news_headline(r)
        macro = r.get("macro_warning")

        if r.get("take_trade"):
            lines.append(f"  #{rank}  ★ {sym} — {direction}  (score {score}, prob {prob})")
            lines.append(f"      price={price}  ATR(14)={atr}")
            if intraday_line:
                lines.append(f"      Intraday:      {intraday_line}")
            if align:
                lines.append(f"                     ✓ {align}")
            if news_badge:
                lines.append(f"      {news_badge}")
                if top_news:
                    lines.append(f"        {top_news}")
            if macro:
                lines.append(f"      ⚠ {macro}")
            lines.append(f"      Entry zone:    {_fmt(r['entry_zone_low'])} – {_fmt(r['entry_zone_high'])}  (mid {_fmt(r['entry'])})")
            lines.append(f"      Stop loss:     {_fmt(r['stop_loss'])}")
            lines.append(f"      TP1 / TP2:     {_fmt(r['take_profit_1'])}  /  {_fmt(r['take_profit_2'])}")
            lines.append(f"      R:R:           {r['rr_ratio']}")
            lines.append(f"      Best window:   {r['best_window']}")
            for w in r.get("warnings", []) or []:
                if w == macro:
                    continue  # already rendered above
                lines.append(f"      ⚠ {w}")
        else:
            lines.append(f"  #{rank}  {sym} — no trade  (score {score}, dir {direction}, prob {prob})")
            lines.append(f"      price={price}  ATR(14)={atr}")
            if intraday_line:
                lines.append(f"      Intraday:  {intraday_line}")
            if news_badge:
                lines.append(f"      {news_badge}")
                if top_news:
                    lines.append(f"        {top_news}")
            if macro:
                lines.append(f"      ⚠ {macro}")
            note = (r.get("reasoning") or "").strip()
            if note:
                lines.append(f"      {note[:120]}")

    lines.append("")
    lines.append(f"  ({len(results) - len(top3)} other symbols scored below the top 3 — see scan summary or dashboard.)")
    lines.append(bar)
    return "\n".join(lines)


def render_scan_summary(results: list[dict[str, Any]], min_mention: int) -> str:
    if not results:
        return "No symbols scanned."
    bar = "─" * 60
    lines: list[str] = [bar, f"  Daily scan — {len(results)} symbols", bar]
    sorted_r = sorted(results, key=lambda r: r.get("confluence_score", 0), reverse=True)
    for r in sorted_r:
        score = r.get("confluence_score", 0)
        symbol = r.get("symbol", "?")
        direction = (r.get("direction") or "").upper() or "—"
        bias = r.get("htf_bias", "?")
        if score >= min_mention:
            tag = "★ WATCH" if score >= ANALYSIS_MIN_SCORE else "    note"
        else:
            tag = "      —"
        lines.append(f"  {tag}  {symbol:<7}  score {score:>3}  dir {direction:<5}  bias {bias}")
    lines.append(bar)
    return "\n".join(lines)
