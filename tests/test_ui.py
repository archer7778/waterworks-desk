"""M1's acceptance test, in a real browser: someone new sets up a region, a system, a site (placed
by clicking the map, heights typed as measured) and a sensor by hand, puts them live, types in a
reading past a limit, and sees a plain-language alert on the overview and in the audit trail.

Needs Playwright with Chromium (python -m pip install playwright; playwright install chromium).
"""
import threading
from http.server import ThreadingHTTPServer

import pytest
from helpers import make_env

pw = pytest.importorskip("playwright.sync_api")

from waterworks_desk import logchain, server  # noqa: E402


@pytest.fixture()
def console():
    tmp, cfg, conn = make_env("""
[[sources]]
key = "exports"
type = "csv"
path = "data/import"
""")
    cfg.data["map"]["tile_url"] = ""      # no network in tests; the map still takes clicks
    app = server.App(cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", conn
    httpd.shutdown()


def fill(page, values):
    for name, v in values.items():
        el = page.locator(f'#dlgForm [name="{name}"]')
        if el.evaluate("e => e.tagName") == "SELECT":
            el.select_option(v)
        else:
            el.fill(str(v))


def test_new_user_sets_up_a_site_and_sensor_by_hand(console):
    base, conn = console
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1300, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        page.click('nav.tabs button[data-view="setup"]')
        page.wait_for_selector("text=Nothing is set up yet")
        # without a name, nothing is saved
        page.click('[data-new="region"]')
        fill(page, {"key": "riverton", "name": "Riverton", "timezone": "Asia/Singapore", "datum": "Town datum",
                    "lat": "1.30", "lon": "103.80", "radius_km": "30"})
        page.click("#formSave")
        page.wait_for_selector("#toast >> text=Type your name first")
        page.fill("#dlg_you", "Mai Le")                                 # the form asks for it; no need to close it
        page.click("#formSave")
        page.wait_for_selector("#toast >> text=Saved as a draft")
        page.click('[data-new="system"]')
        fill(page, {"key": "rv-cs", "name": "Riverton combined sewer", "network_type": "combined_sewer"})
        page.click("#formSave")
        page.wait_for_selector(".tree >> text=Riverton combined sewer")
        page.click('[data-new="site"][data-sys="rv-cs"]')
        fill(page, {"key": "quay-ps", "name": "Quay Street pumping station", "site_type": "pumping_station",
                    "ground_level_m": "2.1", "floor_level_m": "6.5", "floor_level_m__ref": "below_ground",
                    "overflow_level_m": "5.0", "overflow_level_m__ref": "above_floor", "area": "12"})
        page.click("#pickmap", position={"x": 200, "y": 110})           # place it by clicking the map
        assert page.locator('#dlgForm [name="lat"]').input_value() != ""
        assert "Overflow" in page.locator("#sketch").inner_text()       # the side view follows the heights
        page.click("#formSave")
        page.wait_for_selector(".tree a >> text=Quay Street pumping station")
        page.click(".tree a >> text=Quay Street pumping station")
        page.wait_for_selector("h1 >> text=Quay Street pumping station")
        page.click("#addSens")
        fill(page, {"key": "quay-wl", "name": "Wet well level", "quantity": "level", "source": "manual",
                    "source_unit": "m", "display_unit": "m", "mount_level_m": "0.3", "mount_level_m__ref": "above_floor",
                    "warn_hi": "5", "alarm_hi": "4"})                    # warning above alarm: refused in plain words
        page.click("#formSave")
        page.wait_for_timeout(800)
        probs = page.locator("#formProblems").inner_text()
        assert "must be below the alarm hi limit" in probs, probs
        fill(page, {"warn_hi": "4.0", "alarm_hi": "4.6"})
        page.click("#formSave")
        page.wait_for_selector("tr[data-sensor='quay-wl']")
        page.click('nav.tabs button[data-view="setup"]')
        page.click('[data-live="system:rv-cs"]')
        page.wait_for_selector("#toast >> text=are now live")
        page.click(".tree a >> text=Quay Street pumping station")
        page.click("#manual")
        page.fill("#m_v", "4.3")
        page.click("#m_save")
        page.wait_for_selector("#toast >> text=Reading saved")
        page.click('nav.tabs button[data-view="overview"]')
        page.wait_for_selector(".alert.warning")
        text = page.locator(".alert.warning").first.inner_text()
        # 4.3 m above a zero point 0.3 m above the floor is 0.4 m below the overflow (floor + 5 m): within the
        # 0.5 m margin. (The limit alert needs two readings in a row; one was typed.)
        assert "Wet well level at Quay Street pumping station is 4.30 m, within 0.50 m of the overflow level" in text
        page.click("nav.tabs button[data-view='log']")
        page.wait_for_selector("text=Chain verified")
        assert page.locator("td >> text=Mai Le").count() >= 5
        page.screenshot(path="/tmp/wwd-ui-test.png", full_page=True)
        b.close()
    assert not errors, errors
    assert logchain.verify(conn)["ok"]
    assert conn.execute("SELECT status FROM sensors WHERE key='quay-wl'").fetchone()[0] == "live"
    lvl = conn.execute("SELECT mount_level_m FROM sensors WHERE key='quay-wl'").fetchone()[0]
    assert abs(lvl - (2.1 - 6.5 + 0.3)) < 1e-9                          # 0.3 m above a floor 6.5 m below ground
