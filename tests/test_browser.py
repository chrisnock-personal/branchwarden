"""
A browser smoke test (Playwright, Chromium): first-run setup, then every
section of the dashboard with the demo data, clicking through as a person
would, and a few forms. Needs Postgres (TEST_DATABASE_URL) and BROWSER_TESTS=1;
CI runs it.

    pip install -r requirements-dev.txt && python -m playwright install chromium
    BROWSER_TESTS=1 TEST_DATABASE_URL=postgresql://… python -m pytest tests/test_browser.py
"""

import os
import threading

import pytest

pytestmark = pytest.mark.skipif(os.getenv("BROWSER_TESTS") != "1", reason="set BROWSER_TESTS=1 (needs Chromium)")


@pytest.fixture
def server(db_url):
    from werkzeug.serving import make_server

    import admin
    import dashboard
    import db
    import demo
    import settings
    with db.get_conn() as conn:
        demo.load(conn, settings.load(conn))
    app = dashboard.app
    app.secret_key = "s" * 32
    app.config["SESSION_COOKIE_SECURE"] = False      # plain HTTP on localhost, for the test only
    admin._setup_code.clear()
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_setup_sign_in_and_every_section(server):
    from playwright.sync_api import expect, sync_playwright

    import admin
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # First run: the setup page asks for the code from the log.
        page.goto(server + "/")
        expect(page).to_have_url(server + "/setup")
        page.fill("#code", admin._the_setup_code())
        page.fill("#name", "Dana")
        page.fill("#email", "dana@demo-org.example")
        page.fill("#password", "a-long-test-password")
        page.click("button[type=submit]")
        page.wait_for_load_state()
        assert "/setup" not in page.url

        # Every section from the header, each with the demo banner and no error page.
        for label, heading in (("Overview", "Overview"), ("Pull requests", "Pull requests"), ("Branches", "Branches"),
                               ("Repos", "Repositories"), ("Workflows", "Workflows"), ("Metrics", "Metrics"),
                               ("MVP", "MVP"), ("Settings", "GitHub")):
            page.click(f"nav.sections >> text={label}")
            expect(page.locator("h1")).to_have_text(heading)
            expect(page.locator(".worker-banner").first).to_contain_text("Demo data.")

        # Into a repository from the overview's cards, and from there to its pull requests.
        page.click("nav.sections >> text=Overview")
        page.click(".repo .repo-main >> text=billing-api")
        expect(page.locator("h1")).to_have_text("billing-api")
        expect(page.locator("text=Head branches aren't deleted after merge")).to_be_visible()
        page.click("text=On the Pull requests page")
        expect(page.locator("h1")).to_have_text("Pull requests")

        # Filters, a workflow row opening, the theme switch.
        page.goto(server + "/pulls")
        page.click(".chip >> text=Stalled")
        expect(page.locator("tbody tr")).to_have_count(2)
        page.goto(server + "/workflows")
        page.click("summary.wf-row >> text=Nightly build")
        expect(page.locator("details.wf[open] table")).to_be_visible()
        page.click("[data-theme-toggle] >> nth=0")
        assert page.evaluate("document.documentElement.dataset.theme") == "light"

        # Forms: ignore a stale branch, then find it under Ignored branches.
        page.goto(server + "/branches")
        row = page.locator("tbody tr", has_text="tmp/backfill-2026")
        row.locator("button >> text=Ignore").click()
        expect(page.locator(".flash.ok")).to_contain_text("won't be flagged again")
        # My work, from the menu under the name.
        page.click("summary.who")
        page.click(".menu >> text=My work")
        expect(page.locator("h1")).to_contain_text("My work")
        browser.close()
    assert not errors, errors
