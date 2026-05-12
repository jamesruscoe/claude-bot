"""Playwright-based TradingView chart screenshots.

Confirmation step for the scanner. When a symbol scores at or above
CHART_CAPTURE_MIN_SCORE, scan.py calls capture_charts() to grab Daily,
4H, and 1H TradingView views. The PNGs land in charts/ and are uploaded
as a GitHub Actions artifact so the human reviewer can eyeball price
action before acting on the alert — no extra API calls required.

TradingView replaces an earlier Yahoo Finance flow which broke whenever
Yahoo redesigned its chart UI. TradingView's URL params (?symbol=&interval=)
are stable and the chart wrapper has had a consistent selector for years.

The capture is best-effort. If TradingView changes its markup or the
session is rate-limited, individual timeframe failures are tolerated —
partial results are returned. Full failures (browser launch, playwright
missing) return [] without raising.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("chart_capture")


def _install_chromium_fallback() -> bool:
    """Run `python -m playwright install chromium` against the current
    interpreter. Using sys.executable avoids the Windows PATH gotcha where
    `playwright install` resolves to a different Python's venv. Returns
    True on success.
    """
    log.info("attempting playwright chromium install via %s", sys.executable)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("playwright install subprocess failed: %s", e)
        return False
    if result.returncode != 0:
        log.warning(
            "playwright install exited %s: %s",
            result.returncode,
            (result.stderr or result.stdout or "").strip(),
        )
        return False
    return True


CHART_CAPTURE_MIN_SCORE = 75
CHART_DIR = Path("charts")

# Label → TradingView `interval` URL parameter. D / W are alphabetic;
# intraday intervals are encoded as minute counts (60 = 1H, 240 = 4H).
_TIMEFRAMES: dict[str, str] = {
    "D":  "D",
    "4H": "240",
    "1H": "60",
}

# Realistic desktop UA. TradingView serves a lighter mobile chart to
# anything UA-sniffed as mobile; we want the full desktop layout so the
# screenshot matches what a human reviewer sees.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Selectors we'll try for the chart canvas, in priority order. TradingView
# has historically shipped these class names; we try a few in case the
# primary changes.
_CANVAS_SELECTORS = (
    'canvas.chart-gui-canvas',
    'canvas[data-name="pane-top-canvas"]',
    '.chart-container canvas',
    '.layout__area--center canvas',
)

# Selectors for the element we screenshot. We want the chart pane only,
# not the navbar/toolbars. Falls back to the page viewport screenshot if
# none match.
_CHART_AREA_SELECTORS = (
    '.chart-container',
    '.layout__area--center',
    '#header-toolbar-symbol-search ~ div',  # last-ditch fallback
)


async def _wait_for_chart(page, timeout_ms: int = 25_000) -> bool:
    """Wait until at least one chart canvas is attached. Returns True on
    success, False if every selector timed out — caller decides whether
    to screenshot anyway."""
    for selector in _CANVAS_SELECTORS:
        try:
            await page.wait_for_selector(selector, timeout=timeout_ms, state="attached")
            return True
        except Exception:
            continue
    return False


async def _locate_chart_area(page):
    """Return a Locator for the chart pane, or None if no known selector
    matched. The caller should screenshot the viewport if this returns
    None — better a noisier image than no image."""
    for selector in _CHART_AREA_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.count() > 0:
                return locator
        except Exception:
            continue
    return None


async def _dismiss_overlays(page) -> None:
    """Best-effort dismissal of the sign-in / welcome / cookie dialogs
    TradingView occasionally throws at fresh-session visitors. Each click
    is wrapped individually because none of these dialogs are guaranteed
    to show, and a missing element is not a failure."""
    # Cookie banner first — TradingView's EU-style banner sits at the
    # bottom of the chart and crowds the volume bars in the screenshot
    # until it's dismissed.
    cookie_candidates = (
        'button[data-name="cookie-consent-decline-all"]',
        'button[data-name="cookie-consent-allow-all"]',
        'button:has-text("Don’t allow")',
        'button:has-text("Accept all")',
    )
    for selector in cookie_candidates:
        try:
            await page.click(selector, timeout=1_500)
            break
        except Exception:
            continue

    dialog_candidates = (
        'button[data-name="close"]',
        'div.tv-dialog__close',
        'span.tv-dialog__close',
        'button[aria-label="Close"]',
    )
    for selector in dialog_candidates:
        try:
            await page.click(selector, timeout=1_500)
        except Exception:
            pass


async def capture_charts(symbol: str, tradingview_symbol: str | None = None) -> list[str]:
    """Capture D / 4H / 1H TradingView charts for ``symbol``.

    ``tradingview_symbol`` is the fully-qualified exchange-prefixed ticker
    (e.g. "NASDAQ:SMCI", "NYMEX:CL1!"). If omitted we fall back to bare
    ``symbol`` which TradingView will resolve only for unambiguous tickers.

    Returns the saved file paths in capture order, or [] if the run could
    not produce any output (playwright missing, browser launch failure).
    Individual timeframe failures are tolerated.
    """
    tv_symbol = tradingview_symbol or symbol
    CHART_DIR.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        # Most common cause on Windows: package installed but chromium binary
        # missing. Try a one-shot install using sys.executable so we hit the
        # same Python that just failed the import, then retry once.
        if not _install_chromium_fallback():
            log.warning(
                "playwright unavailable and install fallback failed — "
                "skipping chart capture for %s", symbol,
            )
            return []
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            log.warning(
                "playwright still not importable after install fallback (%s) — "
                "skipping chart capture for %s", e, symbol,
            )
            return []

    paths: list[str] = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    viewport={"width": 1600, "height": 900},
                    user_agent=_USER_AGENT,
                    # TradingView pushes a locale-dependent welcome modal —
                    # an explicit locale keeps the run deterministic.
                    locale="en-US",
                )
                page = await context.new_page()

                for label, interval in _TIMEFRAMES.items():
                    url = (
                        f"https://www.tradingview.com/chart/"
                        f"?symbol={tv_symbol}&interval={interval}"
                    )
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                        await _dismiss_overlays(page)
                        canvas_ok = await _wait_for_chart(page)
                        # Even if the selector didn't match in time, give the
                        # SPA a moment to settle — sometimes the chart paints
                        # before the canvas class lands.
                        await asyncio.sleep(3)
                        chart_area = await _locate_chart_area(page)
                        out = CHART_DIR / f"{symbol}_{label}.png"
                        if chart_area is not None:
                            await chart_area.screenshot(path=str(out))
                        else:
                            # Last-ditch: capture the viewport so the human
                            # reviewer at least gets the toolbar context.
                            await page.screenshot(path=str(out), full_page=False)
                        paths.append(str(out))
                        if canvas_ok:
                            log.info("Captured %s %s chart → %s", symbol, label, out)
                        else:
                            log.warning(
                                "Captured %s %s but canvas selector never matched — "
                                "screenshot may be incomplete",
                                symbol, label,
                            )
                    except Exception as e:
                        log.warning("Chart capture failed for %s %s: %s", symbol, label, e)
            finally:
                await browser.close()
    except Exception as e:
        log.exception("Chart capture aborted for %s: %s", symbol, e)
        return []

    return paths
