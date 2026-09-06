import asyncio
from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        # 1. Use actual Google Chrome and disable automation flags
        browser = await p.chromium.launch(
            headless=False,
            channel="chrome",  # Uses your system's real Chrome browser
            args=["--disable-blink-features=AutomationControlled"],
        )

        context = await browser.new_context(viewport={"width": 1440, "height": 900})

        # 2. Inject a script to hide the webdriver flag from Cloudflare
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = await context.new_page()

        print("Navigating to SWAYAM main page...")
        await page.goto("https://swayam.gov.in/")

        print("\n" + "=" * 60)
        print("ACTION REQUIRED: Click 'Sign-In / Register' in the browser.")
        print("Log into your account until you reach your dashboard.")
        print("Once you see your courses, come back to this terminal and press ENTER.")
        print("=" * 60 + "\n")

        input("Press ENTER after you have completed logging in...")

        # Save cookies and local storage state to file
        await context.storage_state(path="state.json")
        print("Authentication state saved to state.json successfully!")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
