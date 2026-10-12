"""Source checks for the rebuilt UI (no frontend test runner in the repo).

Covers the earlier "warning stuck at the top and not fully visible" regression for the new
shell: page banners render in normal flow inside <main> below the sticky header, wrap long
text, and toasts sit at the bottom (never over the header).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
CSS = (SRC / "styles.css").read_text(encoding="utf-8")
SHELL = (SRC / "layouts/AppShell.tsx").read_text(encoding="utf-8")
BANNER = (SRC / "components/features-source/FeaturesSourceBanner.tsx").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    rules = re.findall(r"(?:^|\n|\})\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert rules, selector
    return rules[-1]


def test_page_banners_render_in_flow_below_the_header():
    header_end = SHELL.index("</header>")
    main = SHELL.index('<main className="content">')
    stack = SHELL.index('<div className="banner-stack">')
    assert header_end < main < stack < SHELL.index("<Outlet />")
    assert "position" not in _rule(".banner-stack") and "overflow" not in _rule(".banner")


def test_banners_and_toasts_wrap_and_toasts_never_cover_the_header():
    assert "overflow-wrap: anywhere" in _rule(".banner-body")
    assert "overflow-wrap: anywhere" in _rule(".toast span")
    toasts = _rule(".toasts")
    assert "bottom:" in toasts and "top:" not in toasts
    ui = (SRC / "components/ui/index.tsx").read_text(encoding="utf-8")
    assert 'aria-label="Dismiss"' in ui and "onDismiss" in ui


def test_rescan_banner_collapses_but_stays_while_relevant():
    assert "collapsedKey === mismatchKey" in BANNER
    assert "Rescan now" in BANNER and "onDismiss" not in BANNER[: BANNER.index("Rescan now")]


def test_monitoring_and_admin_are_separate_route_trees():
    app = (SRC / "App.tsx").read_text(encoding="utf-8")
    assert '<Route path="/monitor" element={<MonitorLayout />}>' in app
    assert '<Route path="/admin" element={<AdminLayout />}>' in app
    for path in ("machines/:id", "live", "settings/source", "backfill", "operations", "diagnostics"):
        assert f'path="{path}"' in app, path
    admin = (SRC / "layouts/AdminLayout.tsx").read_text(encoding="utf-8")
    assert 'mode="admin"' in admin and "ADMIN" in admin
    assert '[data-mode="admin"] { --mode: var(--admin); }' in CSS


def test_features_source_switch_lives_in_monitoring_only():
    monitor = (SRC / "layouts/MonitorLayout.tsx").read_text(encoding="utf-8")
    assert '<FeaturesSourceControl placement="header" canSwitch />' in monitor and "<FeaturesSourceBanner />" in monitor
    admin_files = [*(SRC / "pages/admin").rglob("*.tsx"), SRC / "layouts/AdminLayout.tsx"]
    for path in admin_files:
        text = path.read_text(encoding="utf-8")
        assert "FeaturesSource" not in text and "features-source" not in text, path.name
    control = (SRC / "components/features-source/FeaturesSourceControl.tsx").read_text(encoding="utf-8")
    assert "placement: 'header' | 'page'; canSwitch: boolean" in control and "ConfirmDialog" in control


def test_sandbox_campaigns_cost_logs_and_charts_are_gone():
    text = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*") if p.suffix in {".ts", ".tsx", ".css"})
    for word in ("VITE_ENABLE_SANDBOX", "SANDBOX_ENABLED", "Sandbox", "campaign", "CurrentCost", "LogViewer", "recharts"):
        assert word not in text, word
    package = (ROOT / "package.json").read_text(encoding="utf-8")
    assert "recharts" not in package
    assert "VITE_ENABLE_SANDBOX" not in (ROOT / ".env.example").read_text(encoding="utf-8")
