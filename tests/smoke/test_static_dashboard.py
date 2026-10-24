"""Headless-browser smoke test of the static GitHub Pages dashboard.

Serves ``dashboard/static`` (or ``$DASHBOARD_URL`` when set, e.g. the deployed
Pages site) and checks: the page initialises without JavaScript errors, every tab
renders its charts, the city strip re-renders KPIs, and the NPV sliders update
the client-side NPV.

Map styles/tiles are fetched from the internet. In an offline environment set
``SMOKE_OFFLINE_MAPS=1`` to ignore only MapLibre's "Map error." (style download
failure); CI runs online and stays strict.
"""
from __future__ import annotations

import functools
import http.server
import os
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke
STATIC = Path(__file__).resolve().parents[2] / "dashboard" / "static"
playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def base_url():
    if os.environ.get("DASHBOARD_URL"):
        yield os.environ["DASHBOARD_URL"].rstrip("/")
        return
    if not (STATIC / "data" / "payload.json").exists():
        pytest.skip("payload.json not built - run the pipeline first")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(STATIC))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def page(base_url):
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover - environment without browsers
            pytest.skip(f"Chromium not available: {exc}")
        pg = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        offline = os.environ.get("SMOKE_OFFLINE_MAPS") == "1"
        pg.on("pageerror", lambda e: None if (offline and str(e).strip() == "Map error.") else errors.append(str(e)))
        pg.goto(base_url + "/index.html")
        pg.wait_for_function("window.__dashboardReady === true", timeout=30_000)
        pg.errors = errors
        yield pg
        browser.close()


def test_page_initialises_without_js_errors(page):
    assert page.locator("#cityStrip button").count() == 5
    assert page.locator(".kpi").count() == 6
    assert page.locator("#scorecard tbody tr").count() == 5
    assert page.locator("#tabs .dot.good, #tabs .dot.watch, #tabs .dot.breach").count() == 6
    assert page.errors == []


@pytest.mark.parametrize("tab,selector", [("overview", "#ovCost"), ("routes", "#zoneBar"), ("fleet", "#fleetCurve"),
                                          ("warehouse", "#heat"), ("simulation", "#simBar"), ("network", "#npvBars")])
def test_each_tab_renders_charts(page, tab, selector):
    page.click(f'#tabs button[data-tab="{tab}"]')
    page.wait_for_selector(f"{selector} .main-svg", timeout=15_000)
    assert page.locator(f"#panel-{tab}").is_visible()
    assert page.errors == []


def test_city_switch_updates_kpis(page):
    page.click('#tabs button[data-tab="overview"]')
    before = page.inner_text("#kpis")
    page.click('#scorecard tr[data-city="DEL"]')
    assert page.get_attribute('#cityStrip button[data-city="DEL"]', "aria-pressed") == "true"
    page.click('#cityStrip button[data-city="KOL"]')
    assert page.get_attribute('#cityStrip button[data-city="KOL"]', "aria-pressed") == "true"
    assert page.inner_text("#kpis") != before


def test_npv_slider_recomputes_client_side(page):
    page.click('#tabs button[data-tab="network"]')
    before = page.inner_text("#kpis")
    page.fill("#waccSlider", "9")
    page.dispatch_event("#waccSlider", "input")
    assert page.inner_text("#waccOut") == "9.0%"
    assert page.inner_text("#kpis") != before
