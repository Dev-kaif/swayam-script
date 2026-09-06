import os
import json
import asyncio
from urllib.parse import urljoin, parse_qs, urlparse
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


async def main():
    recorded_quizzes = {}
    keep_recording = True

    async def background_recorder(page):
        """Polls the browser URL from Python every second to record new quizzes."""
        while keep_recording:
            try:
                current_url = page.url
                parsed = urlparse(current_url)
                params = parse_qs(parsed.query)

                unit_id = params.get("unitId", [""])[0]
                item_id = params.get("assessmentId", params.get("lessonId", [""]))[0]

                if unit_id and item_id:
                    key = f"{unit_id}_{item_id}"
                    if key not in recorded_quizzes:
                        capture = await page.evaluate("""
                            () => {
                                const container = document.querySelector(
                                    '.assessment-content, .practice-questions, [data-assessment-id]'
                                );
                                if (!container) return "";
                                if (/loading content/i.test(container.textContent || '')) return "";
                                const clone = container.cloneNode(true);
                                clone.querySelectorAll('iframe[src*="youtube"], iframe[src*="player"], video').forEach(v => v.remove());
                                return clone.outerHTML;
                            }
                        """)
                        # Empty captures are retried on the next poll. This avoids
                        # saving the SPA's temporary "Loading content..." shell.
                        if capture and len(capture.strip()) > 200:
                            recorded_quizzes[key] = capture
                            print(
                                f"  [+] Recorded quiz/lesson: '{key}' (Total: {len(recorded_quizzes)})"
                            )
            except Exception:
                pass
            await asyncio.sleep(1)

    async with async_playwright() as p:
        print("Launching browser with Python active background recorder...")
        browser = await p.chromium.launch(
            headless=False,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,820",
            ],
        )

        state_file = "state.json"
        if not os.path.exists(state_file):
            print("Error: state.json not found! Run save_session.py first.")
            return

        context = await browser.new_context(
            # MacBook Air 13-inch (2560×1664 @ 2×): this leaves space for
            # Chrome's toolbar and the macOS menu bar without overflowing.
            viewport={"width": 1280, "height": 720}, storage_state=state_file
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = await context.new_page()
        target_url = "https://onlinecourses.nptel.ac.in/e-learning/course/noc26_cs180"

        print("Navigating to course page...")
        await page.goto(target_url, wait_until="networkidle")
        await page.wait_for_timeout(2000)

        recorder_task = asyncio.create_task(background_recorder(page))

        print("\n" + "=" * 70)
        print("LIVE RECORDING MODE ACTIVE:")
        print("1. Click on each quiz / assignment in the sidebar.")
        print("2. Wait 1-2 seconds on each page until '[+] Recorded...' appears.")
        print("=" * 70 + "\n")

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            input,
            ">>> Once you have recorded all quizzes, press ENTER here... \n",
        )

        keep_recording = False
        await recorder_task

        print(f"\nCaptured {len(recorded_quizzes)} quizzes. Building offline file...")
        raw_html = await page.content()
        soup = BeautifulSoup(raw_html, "html.parser")

        # 1. Offline sidebar styling. Do not force collapsed accordions open:
        # it breaks the original responsive layout and creates empty gaps.
        override_style = soup.new_tag("style")
        override_style.string = """
            a.offline-active-quiz, button.offline-active-quiz {
                background-color: #e8f0fe !important;
                font-weight: bold !important;
                border-left: 4px solid #1a73e8 !important;
            }
            @media (max-width: 1023px) {
                .Sidebar.offline-sidebar-open {
                    display: flex !important;
                    position: fixed !important;
                    z-index: 10001 !important;
                    inset: 0 auto 0 0 !important;
                    width: min(88vw, 380px) !important;
                    margin: 0 !important;
                    padding: 12px 0 12px 12px !important;
                    background: rgba(15, 23, 42, .35) !important;
                }
                .Sidebar.offline-sidebar-open > div {
                    border-radius: 20px !important;
                }
                body.offline-sidebar-open {
                    overflow: hidden !important;
                }
            }
        """
        if soup.head:
            soup.head.append(override_style)

        # 2. Tag target container explicitly
        container_el = soup.select_one(
            '#course-content, .lesson-container, .main-content, main, [role="main"]'
        )
        if container_el:
            container_el["id"] = "nptel-offline-display"

        # 3. Local CSS Asset Downloader
        assets_dir = "assets"
        os.makedirs(assets_dir, exist_ok=True)
        css_tags = soup.find_all("link", rel="stylesheet")
        for idx, tag in enumerate(css_tags):
            href = tag.get("href")
            if not href:
                continue
            abs_css = urljoin(target_url, href)
            css_name = f"style_{idx}.css"
            local_css = os.path.join(assets_dir, css_name)
            try:
                res = await context.request.get(abs_css)
                if res.status == 200:
                    with open(local_css, "w", encoding="utf-8") as f:
                        f.write(await res.text())
                    tag["href"] = f"./assets/{css_name}"
            except Exception:
                pass

        for base in soup.find_all("base"):
            base.decompose()

        # 4. Store payload as JSON, never as JavaScript source. Escaping `</`
        # prevents recorded page markup from closing this script tag early.
        payload = soup.new_tag("script", id="nptel-offline-quizzes", type="application/json")
        payload.string = json.dumps(recorded_quizzes).replace("</", "<\\/")
        if soup.body:
            soup.body.append(payload)

        # 5. Inject button-aware offline navigation. Sidebar entries on SWAYAM are
        # buttons, not anchors, so map each unit to its recorded assessment.
        router_js = soup.new_tag("script")
        router_js.string = """
            (() => {
                const dataElement = document.getElementById('nptel-offline-quizzes');
                const quizzes = dataElement ? JSON.parse(dataElement.textContent) : {};
                const quizByUnit = Object.fromEntries(
                    Object.keys(quizzes).map(key => [key.split('_')[0], key])
                );

                function displayArea() {
                    return document.getElementById('nptel-offline-display')
                        || document.querySelector('#course-content, .lesson-container, .main-content, main')
                        || document.body;
                }

                function notice(message) {
                    let element = document.getElementById('offline-navigation-notice');
                    if (!element) {
                        element = document.createElement('div');
                        element.id = 'offline-navigation-notice';
                        element.setAttribute('role', 'status');
                        element.style.cssText = 'position:fixed;z-index:10000;left:50%;bottom:24px;transform:translateX(-50%);max-width:min(90vw,520px);padding:12px 16px;border-radius:8px;background:#1f2937;color:white;font:500 14px/1.4 system-ui,sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.22);';
                        document.body.appendChild(element);
                    }
                    element.textContent = message;
                    clearTimeout(notice.timer);
                    notice.timer = setTimeout(() => element.remove(), 3500);
                }

                function showQuiz(key, trigger) {
                    if (!quizzes[key]) return notice('This assessment was not captured offline.');
                    displayArea().innerHTML = quizzes[key];
                    document.querySelectorAll('.offline-active-quiz').forEach(item => item.classList.remove('offline-active-quiz'));
                    if (trigger) trigger.classList.add('offline-active-quiz');
                    window.scrollTo({ top: 0, behavior: 'smooth' });
                }

                const sidebar = document.querySelector('.Sidebar');
                const mobileMenuButton = document.querySelector(
                    '[data-slot="sheet-trigger"][aria-label="Open lessons"]'
                );
                function closeMobileSidebar() {
                    if (window.matchMedia('(max-width: 1023px)').matches) {
                        sidebar?.classList.remove('offline-sidebar-open');
                        document.body.classList.remove('offline-sidebar-open');
                        mobileMenuButton?.setAttribute('aria-expanded', 'false');
                    }
                }
                mobileMenuButton?.addEventListener('click', event => {
                    event.preventDefault();
                    const isOpen = sidebar?.classList.toggle('offline-sidebar-open');
                    document.body.classList.toggle('offline-sidebar-open', Boolean(isOpen));
                    mobileMenuButton.setAttribute('aria-expanded', String(Boolean(isOpen)));
                });

                function setSectionState(button, expanded) {
                    const panel = document.getElementById(button.getAttribute('aria-controls'));
                    button.setAttribute('aria-expanded', String(expanded));
                    const icon = button.querySelector('svg');
                    icon?.classList.toggle('rotate-90', expanded);
                    if (panel) {
                        panel.hidden = !expanded;
                        panel.style.display = expanded ? '' : 'none';
                    }
                }

                // Start with a clean, compact outline. Captured pages can retain
                // stale aria/class state from the live React application.
                document.querySelectorAll('button[aria-controls^="unit-"]').forEach(button => {
                    setSectionState(button, false);
                });

                document.addEventListener('click', event => {
                    const target = event.target.closest('button, a');
                    if (!target) return;

                    if (target.matches('button[aria-controls^="unit-"]')) {
                        setSectionState(target, target.getAttribute('aria-expanded') === 'false');
                        return;
                    }

                    const outline = target.closest('nav[aria-label="Course outline"]');
                    if (!outline) return;
                    const unit = target.closest('[id^="unit-"]');
                    const unitId = unit && unit.id.match(/^unit-(\\d+)-list$/)?.[1];
                    const label = target.textContent.replace(/\\s+/g, ' ').trim();
                    if (/assignment|quiz/i.test(label) && quizByUnit[unitId]) {
                        event.preventDefault();
                        showQuiz(quizByUnit[unitId], target);
                        closeMobileSidebar();
                    } else if (/assignment|quiz/i.test(label)) {
                        notice('This assessment was not captured offline.');
                        closeMobileSidebar();
                    }
                }, true);
            })();
        """
        if soup.body:
            soup.body.append(router_js)

        output_file = "nptel_multi_quiz_offline.html"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(str(soup))

        print(f"SUCCESS! Saved working offline bundle to '{output_file}'.")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
