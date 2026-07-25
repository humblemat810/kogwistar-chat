"""Opt-in visual smoke review; run manually, never as regular pytest."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    base_url = os.getenv("CHAT_APP_URL", "http://127.0.0.1:5173").rstrip("/")
    prompt = "Explain the deterministic evidence path."
    expected_answer = "Deterministic answer: evidence is linked to the completed run snapshot."
    artifact_dir = Path(os.getenv("BROWSER_REVIEW_ARTIFACT_DIR", "tests/artifacts/browser-review"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    console_errors: list[str] = []
    failed_requests: list[str] = []
    sse_payloads: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = context.new_page()
        page.on("console", lambda message: console_errors.append(f"{message.text} @ {message.location}") if message.type == "error" else None)
        page.on("requestfailed", lambda request: failed_requests.append(f"{request.method} {request.url}: {request.failure}"))

        def capture_response(response) -> None:
            if "/events/" in response.url:
                try:
                    sse_payloads.append(response.body().decode("utf-8", errors="replace"))
                except Exception:
                    pass

        page.on("response", capture_response)

        page.goto(f"{base_url}/login", wait_until="networkidle")
        page.screenshot(path=str(artifact_dir / "01-login.png"), full_page=True)
        page.get_by_role("button", name="Dev Login (RW Access) ->").click()
        page.wait_for_url(re.compile(rf"^{re.escape(base_url)}/(?:c/[^/?#]+)?(?:[?#].*)?$"))
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(1000)
        page.screenshot(path=str(artifact_dir / "02-home-after-dev-login.png"), full_page=True)
        page.locator("input[name=message]").fill(prompt)
        page.get_by_role("button", name="Send").click()
        page.get_by_text(expected_answer, exact=False).wait_for(timeout=10000)
        page.screenshot(path=str(artifact_dir / "03-completed-answer.png"), full_page=True)
        page.get_by_role("button", name="Inspect run").click()
        page.get_by_role("heading", name="Run fake-run-0001").wait_for(timeout=10000)
        page.locator("summary").filter(has_text="Evidence").click()
        page.get_by_text("fake-node-0001", exact=False).wait_for(timeout=10000)
        page.screenshot(path=str(artifact_dir / "04-run-inspector-evidence.png"), full_page=True)

        report = {
            "url": page.url,
            "title": page.title(),
            "body_text_prefix": page.locator("body").inner_text()[:1000],
            "console_errors": console_errors,
            "failed_requests": failed_requests,
            "fake_prompt": prompt,
            "fake_answer_seen": expected_answer in page.locator("body").inner_text(),
            "evidence_seen": "fake-node-0001" in page.locator("body").inner_text(),
            "terminal_marker_seen": "STATIC | DONE" in page.locator("body").inner_text(),
            "dom_targets": {target: page.locator(target).count() for target in ["#events-list", "#events-window", "#debug-panel-controls"]},
            "sse_oob_targets": sorted(set(re.findall(r'hx-swap-oob="([^"]+)"', "\n".join(sse_payloads)))),
        }
        (artifact_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        context.tracing.stop(path=str(artifact_dir / "trace.zip"))
        browser.close()

    print(json.dumps(report, indent=2))
    return 1 if console_errors or failed_requests else 0


if __name__ == "__main__":
    raise SystemExit(main())
