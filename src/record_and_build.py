import os
import json
import asyncio
from urllib.parse import urljoin, parse_qs, urlparse
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


OUTPUT_DIR = "Working"
OUTPUT_FILE = "nptel_multi_quiz_offline.html"
PROGRESS_OUTPUT_FILE = "nptel_course_progress_offline.html"
FAVICON_FILE = "favicon.png"


def favicon_extension(content_type: str) -> str:
    """Choose a browser-friendly extension for the downloaded icon."""
    content_type = content_type.lower().split(";", 1)[0].strip()
    return {
        "image/png": "png",
        "image/svg+xml": "svg",
        "image/x-icon": "ico",
        "image/vnd.microsoft.icon": "ico",
        "image/jpeg": "jpg",
        "image/webp": "webp",
    }.get(content_type, "ico")


async def main():
    recorded_quizzes = {}
    keep_recording = True
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    def make_file_link(soup, label: str, filename: str):
        """Turn a captured React-only control into a working local file link."""
        for text_node in soup.find_all(
            string=lambda value: value and value.strip() == label
        ):
            control = text_node.parent
            control.name = "a"
            control["href"] = f"./{filename}"
            control["onclick"] = ""

    def link_back_to_course_outline(soup):
        """Make the progress page's SVG-and-text back control work offline."""
        for control in soup.select('[aria-label="Back to Course Outline"]'):
            control.name = "a"
            control["href"] = f"./{OUTPUT_FILE}"
            control["onclick"] = ""

    async def capture_all_outline_panels(page):
        """Load each lazy sidebar week and retain its rendered lesson panel."""
        outline_buttons = page.locator(
            'nav[aria-label="Course outline"] button[aria-controls^="unit-"]'
        )
        panels = {}
        for index in range(await outline_buttons.count()):
            button = outline_buttons.nth(index)
            if not await button.is_visible():
                continue
            panel_id = await button.get_attribute("aria-controls")
            if not panel_id:
                continue
            await button.click(timeout=5_000)
            panel = page.locator(f"#{panel_id}")
            try:
                await panel.wait_for(state="attached", timeout=5_000)
                await page.wait_for_function(
                    """id => {
                        const element = document.getElementById(id);
                        return element && element.textContent.trim().length > 0;
                    }""",
                    panel_id,
                    timeout=5_000,
                )
                panels[panel_id] = await panel.evaluate("element => element.outerHTML")
            except Exception:
                print(f"  [!] Could not load sidebar panel '{panel_id}'.")
        print(f"  [+] Captured {len(panels)} sidebar lesson panels.")
        return panels

    def restore_lazy_panels(soup, panels):
        """Insert lazy React panels that were absent from page.content()."""
        for panel_id, panel_html in panels.items():
            replacement = BeautifulSoup(panel_html, "html.parser").find()
            if replacement is None:
                continue
            header = soup.select_one(f'button[aria-controls="{panel_id}"]')
            if header is None:
                labelled_by = replacement.get("aria-labelledby")
                header = soup.find(id=labelled_by) if labelled_by else None
            if header is None:
                continue
            header["aria-controls"] = panel_id
            existing = soup.find(id=panel_id)
            if existing:
                existing.replace_with(replacement)
            else:
                header.insert_after(replacement)

    async def capture_progress_panels_interactively(page):
        """Save lazy progress panels after the user confirms each is loaded."""
        panels = {}
        loop = asyncio.get_running_loop()
        for label in (
            "Assignment Scores",
            "Unit Wise Progress",
            "Grading and Certifications Policy",
        ):
            await loop.run_in_executor(
                None,
                input,
                f"\n>>> In the Progress browser tab, open '{label}', wait for its "
                "content to load, then press ENTER here... ",
            )
            locator = page.locator('button[data-slot="accordion-trigger"]')
            trigger = None
            for index in range(await locator.count()):
                candidate = locator.nth(index)
                if (
                    await candidate.is_visible()
                    and (await candidate.text_content() or "").strip() == label
                ):
                    trigger = candidate
                    break
            if trigger is None:
                print(f"  [!] Could not find progress section '{label}'.")
                continue
            if await trigger.get_attribute("aria-expanded") != "true":
                print(f"  [!] '{label}' is still closed; open it before confirming.")
                continue
            panel_id = await trigger.get_attribute("aria-controls")
            try:
                if panel_id:
                    panel = page.locator(f"#{panel_id}")
                else:
                    trigger_id = await trigger.get_attribute("id")
                    if not trigger_id:
                        raise RuntimeError("accordion trigger has no id")
                    panel = page.locator(
                        f'[data-slot="accordion-content"][aria-labelledby="{trigger_id}"]'
                    )
                await panel.wait_for(state="attached", timeout=5_000)
                panel_id = await panel.get_attribute("id")
                if not panel_id:
                    raise RuntimeError("accordion panel has no id")
                await page.wait_for_timeout(300)
                panel_html = await panel.evaluate("element => element.outerHTML")
                if len(panel_html) < 200:
                    raise RuntimeError("accordion content was empty")
                panels[panel_id] = panel_html
                print(f"  [+] Captured progress section '{label}'.")
            except Exception as error:
                print(f"  [!] Could not load progress section '{label}': {error}")
        print(f"  [+] Captured {len(panels)} progress panels.")
        return panels

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
        # Capture both views on separate, fresh pages. Their React layouts are
        # different, so embedding one inside the other breaks both sidebars.
        course_capture = await context.new_page()
        await course_capture.goto(target_url, wait_until="networkidle")
        await course_capture.wait_for_timeout(1_000)
        outline_panels = await capture_all_outline_panels(course_capture)
        raw_html = await course_capture.content()

        progress_capture = await context.new_page()
        progress_url = "https://onlinecourses.nptel.ac.in/e-learning/progress/noc26_cs180"
        await progress_capture.goto(progress_url, wait_until="networkidle")
        await progress_capture.get_by_text(
            "Student Progress Report", exact=True
        ).first.wait_for(state="visible", timeout=15_000)
        await progress_capture.bring_to_front()
        print("\n" + "=" * 70)
        print("LIVE PROGRESS CAPTURE MODE ACTIVE:")
        print("Open each requested section in the Progress browser tab and wait")
        print("until its real content appears before confirming in this terminal.")
        print("=" * 70)
        progress_panels = await capture_progress_panels_interactively(progress_capture)
        progress_raw_html = await progress_capture.content()

        soup = BeautifulSoup(raw_html, "html.parser")
        restore_lazy_panels(soup, outline_panels)
        progress_soup = BeautifulSoup(progress_raw_html, "html.parser")
        restore_lazy_panels(progress_soup, progress_panels)

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

        # 2. The course page has no stable <main> node. Target the inner lesson
        # area of the right-hand card, not the card itself: its first child
        # contains the mobile hamburger and assessment header, both of which
        # must remain visible after an offline quiz is opened.
        container_el = None
        layout_root = soup.select_one(".layout-root")
        if layout_root:
            sidebar_el = layout_root.select_one(":scope > .Sidebar")
            course_card = None
            for child in layout_root.find_all(recursive=False):
                if child is not sidebar_el:
                    course_card = child
                    break
            if course_card:
                for child in course_card.find_all(recursive=False):
                    classes = child.get("class", [])
                    if "flex-1" in classes and "flex-col" in classes:
                        container_el = child
                        break
                container_el = container_el or course_card
        if container_el is None:
            container_el = soup.select_one(
                '#course-content, .lesson-container, .main-content, main, [role="main"]'
            )
        if container_el:
            container_el["id"] = "nptel-offline-display"

        make_file_link(soup, "Course Progress", PROGRESS_OUTPUT_FILE)
        link_back_to_course_outline(progress_soup)

        # React's progress accordion has no handlers after page.content() is
        # saved. Recreate just its open/close behavior for the standalone file.
        progress_router = progress_soup.new_tag("script")
        progress_router.string = """
            (() => {
                function setAccordionState(trigger, isOpen) {
                    const panel = (trigger.getAttribute('aria-controls')
                        && document.getElementById(trigger.getAttribute('aria-controls')))
                        || (trigger.id && document.querySelector(
                            `[aria-labelledby="${CSS.escape(trigger.id)}"]`
                        ));
                    const item = trigger.closest('[data-slot="accordion-item"]');
                    trigger.setAttribute('aria-expanded', String(isOpen));
                    trigger.setAttribute('data-state', isOpen ? 'open' : 'closed');
                    item?.setAttribute('data-state', isOpen ? 'open' : 'closed');
                    if (panel) {
                        trigger.setAttribute('aria-controls', panel.id);
                        panel.hidden = !isOpen;
                        panel.style.display = isOpen ? '' : 'none';
                        panel.setAttribute('data-state', isOpen ? 'open' : 'closed');
                    }
                }

                document.addEventListener('click', event => {
                    const trigger = event.target.closest(
                        'button[data-slot="accordion-trigger"]'
                    );
                    if (!trigger) return;
                    event.preventDefault();
                    setAccordionState(
                        trigger,
                        trigger.getAttribute('aria-expanded') !== 'true'
                    );
                });

                // Content is saved for every section, but the standalone page
                // should start compact just like the live progress page.
                document.querySelectorAll(
                    'button[data-slot="accordion-trigger"]'
                ).forEach(trigger => setAccordionState(trigger, false));
            })();
        """
        if progress_soup.body:
            progress_soup.body.append(progress_router)

        # 3. Local CSS Asset Downloader
        assets_dir = os.path.join(OUTPUT_DIR, "assets")
        os.makedirs(assets_dir, exist_ok=True)

        # Download the course favicon so the saved file does not depend on the
        # original website. Keep the existing local icon if this request fails.
        favicon_name = FAVICON_FILE
        favicon_mime = "image/png"
        try:
            favicon_url = await course_capture.evaluate(
                """() => document.querySelector('link[rel~="icon"]')?.href
                    || new URL('/favicon.ico', location.origin).href"""
            )
            favicon_response = await context.request.get(favicon_url)
            if favicon_response.status == 200:
                favicon_mime = favicon_response.headers.get("content-type", "image/x-icon")
                favicon_name = f"favicon.{favicon_extension(favicon_mime)}"
                with open(os.path.join(OUTPUT_DIR, favicon_name), "wb") as f:
                    f.write(await favicon_response.body())
                print(f"  [+] Downloaded favicon: '{favicon_name}'")
        except Exception as error:
            print(f"  [!] Could not download favicon; keeping local fallback: {error}")

        # Always use the favicon stored next to this offline HTML file.
        if soup.head:
            for icon in soup.head.find_all("link", rel=lambda value: value and "icon" in value):
                icon.decompose()
            favicon = soup.new_tag(
                "link", rel="icon", type=favicon_mime, href=f"./{favicon_name}"
            )
            soup.head.insert(0, favicon)
        if progress_soup.head:
            for icon in progress_soup.head.find_all("link", rel=lambda value: value and "icon" in value):
                icon.decompose()
            progress_soup.head.insert(0, progress_soup.new_tag(
                "link", rel="icon", type=favicon_mime, href=f"./{favicon_name}"
            ))

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

        progress_css_tags = progress_soup.find_all("link", rel="stylesheet")
        for idx, tag in enumerate(progress_css_tags):
            href = tag.get("href")
            if not href:
                continue
            abs_css = urljoin(progress_url, href)
            css_name = f"progress_style_{idx}.css"
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
        for base in progress_soup.find_all("base"):
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
                    const panelId = button.getAttribute('aria-controls');
                    // Unit panels are immediate siblings in the saved outline.
                    // Prefer that local relationship so duplicated SPA IDs or
                    // stale React markup cannot make a week appear empty.
                    const panel = button.nextElementSibling
                        || (panelId && document.getElementById(panelId));
                    button.setAttribute('aria-expanded', String(expanded));
                    const icon = button.querySelector('svg');
                    icon?.classList.toggle('rotate-90', expanded);
                    if (panel) {
                        panel.hidden = !expanded;
                        panel.style.display = expanded ? '' : 'none';
                    }
                }

                // Preserve the old, working compact sidebar: every week starts
                // closed and clicking its header expands its own saved content.
                document.querySelectorAll('button[aria-controls^="unit-"]').forEach(button => {
                    setSectionState(button, false);
                });

                document.addEventListener('click', event => {
                    const sectionButton = event.target.closest(
                        'button[aria-controls^="unit-"]'
                    );
                    if (sectionButton) {
                        event.preventDefault();
                        event.stopPropagation();
                        setSectionState(
                            sectionButton,
                            sectionButton.getAttribute('aria-expanded') === 'false'
                        );
                        return;
                    }

                    const target = event.target.closest('button, a');
                    if (!target) return;

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

        output_file = os.path.join(OUTPUT_DIR, OUTPUT_FILE)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(str(soup))

        progress_output_file = os.path.join(OUTPUT_DIR, PROGRESS_OUTPUT_FILE)
        with open(progress_output_file, "w", encoding="utf-8") as f:
            f.write(str(progress_soup))

        print(
            "SUCCESS! Saved offline course and progress pages to "
            f"'{output_file}' and '{progress_output_file}'."
        )
        await course_capture.close()
        await progress_capture.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
