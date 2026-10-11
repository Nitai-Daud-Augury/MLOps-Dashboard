"""Source checks for page-level warning/error banners (no frontend test runner in the repo).

Regression for "the warning error is stuck at the top of the page and not 100%
visible": on the desktop Admin shell the banners were covered by the
position:fixed admin rail and the 100dvh overflow:hidden shell clipped the
workspace; toasts sat at top:24px over the sticky header.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "src/App.tsx").read_text(encoding="utf-8")
CSS = (ROOT / "src/App.css").read_text(encoding="utf-8")
BANNER = (ROOT / "src/components/features-source/FeaturesSourceBanner.tsx").read_text(encoding="utf-8")


def _last_rule(selector: str) -> str:
    rules = re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert rules, selector
    return rules[-1]


def test_page_banners_render_in_one_stack_below_the_header():
    header_end = APP.index("</header>", APP.index('<header className="topbar">'))
    stack = APP.index('<div className="page-banners">')
    assert header_end < stack < APP.index("<FeaturesSourceBanner", stack) < APP.index('className={`tab-content', stack)
    assert APP.index("{apiError ? (", stack) < APP.index("<FeaturesSourceBanner", stack)
    assert 'onClick={() => setApiError(null)}' in APP and 'onClick={() => setAdminError(null)}' in APP


def test_banners_wrap_and_toasts_sit_below_the_real_header():
    assert "overflow-wrap: anywhere" in _last_rule(".notice > span, .notice > div")
    assert "var(--topbar-height" in _last_rule(".toast-container")
    assert "overflow-wrap: anywhere" in _last_rule(".toast span")
    assert "--topbar-height" in (ROOT / "src/hooks/use-topbar-height.ts").read_text(encoding="utf-8")


def test_desktop_admin_shell_no_longer_clips_or_covers_banners():
    desktop = CSS[CSS.rindex("@media (min-width: 1101px)"):]
    assert "overflow: visible" in desktop and "position: relative" in desktop
    assert ".admin-shell .admin-sidebar" in desktop and ".admin-shell .tab-content" in desktop


def test_rescan_banner_collapses_but_stays_while_relevant():
    assert "collapsed ? ' collapsed' : ''" in BANNER
    # Collapsing keys on the mismatch, so a new switch/scan re-expands it.
    assert "collapsedKey === mismatchKey" in BANNER
    assert "Rescan now" in BANNER
