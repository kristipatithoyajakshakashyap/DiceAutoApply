"""
Dice.com Auto-Apply Bot — Production v2
========================================
- Contract + Easy Apply filters only
- Broad queries: roles + states + government + cities
- Target: ~60 applications/hour
- Auto-recovers from crashes, tab deaths, CAPTCHAs
- Sleeps 2am-3am, polls every 60s to wake reliably
- No screenshots, rotating logs, clean dedup
"""

import asyncio
import random
import shutil
from datetime import datetime, timedelta

from patchright.async_api import async_playwright, Page, BrowserContext

import config as cfg
from utils import (
    logger, human_delay, short_delay, micro_delay, break_delay,
    bezier_mouse_move, random_mouse_wander, human_type,
    scroll_to_read, STEALTH_JS, random_viewport, random_user_agent,
    ApplicationTracker, countdown_sleep, classify_contract_subtype,
)


class DiceBot:
    def __init__(self):
        self.tracker   = ApplicationTracker()
        self.page: Page = None
        self.context: BrowserContext = None
        self.browser = None  # only set in incognito mode (non-persistent)
        self.consecutive_fails = 0
        self.used_urls  = set()
        self.batch_applied = 0
        self.batch_target  = random.randint(*cfg.BATCH_SIZE)
        self.keywords: list = []
        self.query_index: int = 0
        self._last_search_url: str = ""

    # ── Entry Point ──────────────────────────────────────────
    async def run(self):
        if not cfg.DICE_EMAIL or not cfg.DICE_PASSWORD:
            logger.error("DICE_EMAIL / DICE_PASSWORD not set in .env")
            return

        self.keywords = cfg.load_keywords()
        logger.info(f"Loaded {len(self.keywords)} keywords from keywords.txt")

        logger.info("=" * 55)
        logger.info("Dice Bot v2 — Contract + Easy Apply — ~60 apps/hr")
        logger.info(f"Keywords: {len(self.keywords)} | Sleep: {cfg.SLEEP_HOUR_START}:00-{cfg.SLEEP_HOUR_END}:00")
        logger.info("=" * 55)

        self.used_urls = self.tracker.get_all_applied_urls()
        logger.info(f"Loaded {len(self.used_urls)} previously applied URLs")

        if cfg.INCOGNITO:
            # True incognito already gets a blank in-memory profile every
            # run — no on-disk profile to wipe here.
            pass
        elif cfg.RESET_COOKIES_ON_START:
            # Opt-in only: a brand-new, history-less profile is itself
            # what triggered Dice's "Something went wrong" crash on
            # every single /jobs load in testing — a returning-looking
            # session (real cookies/consent/history) loads fine.
            shutil.rmtree(cfg.BROWSER_PROFILE_DIR, ignore_errors=True)
            logger.info("Cleared browser profile — starting fresh session (no cookies)")
        cfg.BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        consecutive_crashes = 0

        while True:
            if self._is_sleep_time():
                await self._sleep_until_wake()
                continue

            try:
                await self._run_session()
                consecutive_crashes = 0
            except Exception as e:
                consecutive_crashes += 1
                logger.error(f"Session crashed (#{consecutive_crashes}): {e}", exc_info=True)
                wait = min(30 * consecutive_crashes, 300)
                await countdown_sleep(wait, label="Crash backoff — restarting")
                if consecutive_crashes >= 10:
                    logger.warning("10 consecutive crashes — cooling down 10 min")
                    await countdown_sleep(600, label="Cooling down after 10 crashes")
                    consecutive_crashes = 0

    # ── Session ───────────────────────────────────────────────
    async def _run_session(self):
        viewport = random_viewport()
        ua       = random_user_agent()

        async with async_playwright() as p:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                f"--window-size={viewport['width']},{viewport['height']}",
            ]
            if cfg.INCOGNITO:
                launch_args.append("--incognito")

            context_kwargs = dict(
                viewport=viewport,
                user_agent=ua,
                locale="en-US",
                timezone_id="America/New_York",
                color_scheme=random.choice(["light", "dark", "no-preference"]),
            )

            proxy_kwargs = {"proxy": {"server": cfg.PROXY}} if cfg.PROXY else {}

            # Which browser binary to drive — Chromium's own build,
            # system Chrome (channel="chrome"), or Brave (via its exe).
            browser_kwargs = {}
            if cfg.BROWSER == "chrome":
                browser_kwargs["channel"] = "chrome"
            elif cfg.BROWSER == "brave":
                browser_kwargs["executable_path"] = cfg.BRAVE_PATH

            if cfg.INCOGNITO:
                # No user_data_dir at all — nothing written to disk, every
                # run starts with an empty in-memory profile (no cookies).
                self.browser = await p.chromium.launch(
                    headless=cfg.HEADLESS,
                    args=launch_args,
                    slow_mo=random.randint(15, 40),
                    **browser_kwargs,
                    **proxy_kwargs,
                )
                self.context = await self.browser.new_context(**context_kwargs)
            else:
                self.browser = None
                self.context = await p.chromium.launch_persistent_context(
                    user_data_dir=cfg.BROWSER_PROFILE_DIR,
                    headless=cfg.HEADLESS,
                    args=launch_args,
                    slow_mo=random.randint(15, 40),
                    **context_kwargs,
                    **browser_kwargs,
                    **proxy_kwargs,
                )
            await self.context.add_init_script(STEALTH_JS)

            self.page = (
                self.context.pages[0]
                if self.context.pages
                else await self.context.new_page()
            )
            await self.page.bring_to_front()

            # Close any stray popup tabs that open during apply
            self.context.on("page", self._handle_popup)

            try:
                await self._login()
                await self._main_loop()
            except BotBlockedError:
                logger.error("Bot blocked — closing session to rotate fingerprint")
            except Exception as e:
                logger.error(f"Session error: {e}", exc_info=True)
            finally:
                s = self.tracker.summary()
                logger.info(
                    f"Session done — Applied={s['applied']} "
                    f"Skipped={s['skipped']} Failed={s['failed']}"
                )
                try:
                    await self.context.close()
                except Exception:
                    pass
                if self.browser:
                    try:
                        await self.browser.close()
                    except Exception:
                        pass
                # Minimum cooldown before any browser restart attempt.
                # Prevents rapid-fire relaunch cycles on network dropout
                # (ERR_INTERNET_DISCONNECTED exits _run_session cleanly,
                # so consecutive_crashes never increments — without this
                # sleep the outer loop restarts immediately, causing 70+
                # relaunches per minute).
                await asyncio.sleep(10)

    # ── Popup / New Tab Handler ───────────────────────────────
    def _handle_popup(self, page):
        """Close any external ATS tabs that open on Apply click."""
        async def _close():
            try:
                await asyncio.sleep(1.5)
                logger.info(f"Closing external popup: {page.url}")
                await page.close()
            except Exception:
                pass
        asyncio.ensure_future(_close())

    # ── Session Guard — re-login if kicked out mid-run ────────
    async def _ensure_logged_in(self):
        url = self.page.url.lower()
        if "login" in url or "signin" in url or "sign-in" in url:
            logger.warning("Session expired — re-logging in automatically...")
            await self._login()

    # ── Login ─────────────────────────────────────────────────
    async def _login(self):
        logger.info("Checking login state...")
        await self.page.goto(
            "https://www.dice.com/dashboard",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        await short_delay()

        url = self.page.url.lower()
        if "login" not in url and "signin" not in url:
            logger.info("Already logged in.")
            return

        logger.info("Logging in...")

        email_el = self.page.locator('input[name="email"], input[type="email"]').first
        await email_el.wait_for(state="visible", timeout=15_000)
        await bezier_mouse_move(self.page, random.randint(400, 800), random.randint(300, 500))
        await human_type(email_el, cfg.DICE_EMAIL)
        await short_delay()

        await self.page.locator('button[type="submit"]').first.click()
        await short_delay()

        pw_el = self.page.locator('input[type="password"]').first
        try:
            await pw_el.wait_for(state="visible", timeout=8_000)
            await human_type(pw_el, cfg.DICE_PASSWORD)
            await short_delay()
            await self.page.locator('button[type="submit"]').first.click()
            await short_delay()
        except Exception:
            logger.warning("Password field not found — complete login in browser.")

        # Wait up to 5 min for manual login / 2FA
        for i in range(60):
            await asyncio.sleep(5)
            url = self.page.url.lower()
            if "login" not in url and "signin" not in url and "verify" not in url:
                logger.info("Login successful.")
                return
            if i % 6 == 0 and i > 0:
                logger.info(f"Waiting for login... {i*5}s elapsed")

        raise RuntimeError("Login timed out after 5 minutes")

    # ── Main Loop ─────────────────────────────────────────────
    async def _main_loop(self):
        page_num = 1
        current_query = self.keywords[self.query_index]
        consecutive_skipped_pages = 0

        while True:
            if self._is_sleep_time():
                return

            # Batch break
            if self.batch_applied >= self.batch_target:
                mins = random.uniform(*cfg.BATCH_BREAK_MINUTES)
                await countdown_sleep(
                    mins * 60,
                    label=f"Batch of {self.batch_applied} done — resting",
                )
                self.batch_target  = random.randint(*cfg.BATCH_SIZE)
                self.batch_applied = 0
                self.query_index = (self.query_index + 1) % len(self.keywords)
                current_query = self.keywords[self.query_index]
                page_num = 1
                consecutive_skipped_pages = 0

            # Too many consecutive fails — rotate to next keyword
            if self.consecutive_fails >= cfg.MAX_CONSECUTIVE_FAILS:
                logger.warning("Too many failures — rotating keyword and pausing 90s")
                self.consecutive_fails = 0
                self.query_index = (self.query_index + 1) % len(self.keywords)
                current_query = self.keywords[self.query_index]
                page_num = 1
                consecutive_skipped_pages = 0
                await countdown_sleep(90, label="Pausing after repeated failures")

            # Page watchdog
            if not await self._ensure_page_alive():
                logger.error("Page unrecoverable — restarting session")
                return

            # Re-login if session was dropped
            await self._ensure_logged_in()

            logger.info(f"Query: '{current_query}' | Page {page_num}")
            await self._run_search(current_query, page_num)

            jobs = await self._collect_jobs()
            if not jobs:
                logger.info(f"No jobs on page {page_num} — switching to next keyword")
                self.query_index = (self.query_index + 1) % len(self.keywords)
                current_query = self.keywords[self.query_index]
                page_num = 1
                consecutive_skipped_pages = 0
                await asyncio.sleep(random.uniform(3, 8))
                continue

            random.shuffle(jobs)
            max_this_page  = random.randint(5, 10)
            applied_this_page = 0

            for job in jobs:
                if self._is_sleep_time():
                    return
                if applied_this_page >= max_this_page:
                    break
                if self.batch_applied >= self.batch_target:
                    break
                if self.consecutive_fails >= cfg.MAX_CONSECUTIVE_FAILS:
                    break
                if job["url"] in self.used_urls:
                    continue

                self.used_urls.add(job["url"])
                await self._apply_to_job(job)
                applied_this_page += 1
                self.batch_applied += 1

                if (
                    self.tracker.session_count > 0
                    and self.tracker.session_count % cfg.BREAK_EVERY_N_APPS == 0
                ):
                    await break_delay()

                await human_delay()
                await random_mouse_wander(self.page)

            # Exhaustion detection: jobs exist but all already applied to
            if applied_this_page == 0 and len(jobs) > 0:
                consecutive_skipped_pages += 1
                logger.debug(
                    f"All jobs on page {page_num} already applied — "
                    f"skipped page {consecutive_skipped_pages}/3"
                )
                if consecutive_skipped_pages >= 3:
                    logger.info(
                        f"Keyword '{current_query}' exhausted "
                        f"(3 consecutive all-skipped pages) — rotating"
                    )
                    self.query_index = (self.query_index + 1) % len(self.keywords)
                    current_query = self.keywords[self.query_index]
                    page_num = 1
                    consecutive_skipped_pages = 0
                    await asyncio.sleep(random.uniform(3, 8))
                    continue
            else:
                consecutive_skipped_pages = 0

            # Short rest then next page
            rest = random.uniform(3, 8)
            logger.info(f"Next page in {rest:.0f}s...")
            await asyncio.sleep(rest)
            page_num += 1

    # ── Search ────────────────────────────────────────────────
    SEARCH_INPUT_SELECTORS = [
        "#typeaheadInput",
        'input[name="q"]',
        'input[data-testid="search-input"]',
        'input[placeholder*="Job title" i]',
        'input[aria-label*="search" i]',
    ]
    SEARCH_SUBMIT_SELECTORS = [
        "#submitSearch-mainSearchForm",
        'button[data-testid="search-submit"]',
        'button[type="submit"]:has-text("Search")',
        'button[aria-label*="search" i]',
    ]

    async def _run_search(self, keyword: str, page: int = 1):
        """Load the results URL with `q=` included (Dice's results page
        500s without it — confirmed by testing: dropping q from the URL
        made the "Something went wrong" page fire on every single load,
        instantly, before hydration — a server error, not a client
        fingerprint issue), then also type the same keyword into the
        visible search box and click Search. That second step is purely
        cosmetic/confirmatory — it's what lets you watch what's being
        searched — but the URL's own `q=` is what actually has to be
        correct for the page to render."""
        params = f"?q={keyword.replace(' ', '+')}"
        if cfg.JOB_LOCATION:
            params += f"&location={cfg.JOB_LOCATION.replace(' ', '+')}"

        # Workplace type — repeat param once per selected type, but only
        # when it's a real subset. Sending all 3 values as duplicate
        # params (what a real user filtering by hand would never do —
        # they'd just leave the filter alone) is the same as "no
        # filter" and was confirmed by testing to make Dice's own
        # results page 500 instantly on every load.
        workplace_map = {"onsite": "On-Site", "hybrid": "Hybrid", "remote": "Remote"}
        if 0 < len(cfg.WORKPLACE_TYPES) < len(workplace_map):
            for wt in cfg.WORKPLACE_TYPES:
                value = workplace_map.get(wt)
                if value:
                    params += f"&filters.workplaceTypes={value.replace(' ', '+')}"

        # Employment type — Dice's employmentType facet only knows
        # FULLTIME / CONTRACTS (no W2 vs C2C distinction server-side).
        # W2/C2C subtype gating happens per-job in _apply_to_job() by
        # reading the job detail page text. Same rule as above: only
        # send the filter when it actually narrows something.
        wants_contract = any(jt.startswith("contract") for jt in cfg.JOB_TYPES)
        wants_fulltime = "fulltime" in cfg.JOB_TYPES
        if wants_contract and not wants_fulltime:
            params += "&filters.employmentType=CONTRACTS"
        elif wants_fulltime and not wants_contract:
            params += "&filters.employmentType=FULLTIME"

        if cfg.EASY_APPLY_ONLY:
            params += "&filters.easyApply=true"
        params += f"&page={page}"

        url = f"https://www.dice.com/jobs{params}"
        self._last_search_url = url
        logger.info(f"Loading search: {keyword} (page {page})")

        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception:
            # Fallback — just wait for network to settle a bit
            await asyncio.sleep(3)

        await self._recover_from_app_error()
        await short_delay()

        # Now type the keyword into the search box and click Search —
        # visible on screen, same as a human searching.
        search_box = None
        for sel in self.SEARCH_INPUT_SELECTORS:
            loc = self.page.locator(sel).first
            try:
                if await loc.is_visible(timeout=1_500):
                    search_box = loc
                    break
            except Exception:
                continue

        if search_box:
            logger.info(f"Typing search query: '{keyword}'")
            try:
                await search_box.click()
                await search_box.fill("")
                await human_type(search_box, keyword)
                await micro_delay()

                clicked = False
                for sel in self.SEARCH_SUBMIT_SELECTORS:
                    btn = self.page.locator(sel).first
                    try:
                        if await btn.is_visible(timeout=1_000):
                            box = await btn.bounding_box()
                            if box:
                                await bezier_mouse_move(
                                    self.page,
                                    int(box["x"] + box["width"] / 2),
                                    int(box["y"] + box["height"] / 2),
                                )
                            await micro_delay()
                            await btn.click()
                            clicked = True
                            break
                    except Exception:
                        continue

                if not clicked:
                    await search_box.press("Enter")

                await self.page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception as e:
                logger.warning(f"Search box interaction failed, query stays as-is: {e}")
        else:
            logger.warning("Search box not found — showing unfiltered/prior results")

        await self._recover_from_app_error()
        await short_delay()
        await scroll_to_read(self.page)

    # ── Collect Jobs ──────────────────────────────────────────
    async def _collect_jobs(self) -> list:
        jobs = []
        seen_urls = set()

        try:
            await self.page.wait_for_selector(
                'a[data-testid="job-search-job-detail-link"]',
                timeout=12_000,
            )
        except Exception:
            return jobs

        raw = await self.page.evaluate("""() => {
            const links = document.querySelectorAll('a[data-testid="job-search-job-detail-link"]');
            const results = [];
            for (const a of links) {
                const title = (a.getAttribute('aria-label') || a.innerText || '').trim();
                const href = a.href;
                if (title && href) results.push({ title, url: href });
            }
            return results;
        }""")

        for item in raw:
            url = item["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            jobs.append({
                "title":    item["title"],
                "company":  "",
                "location": "",
                "url":      url,
            })

        logger.info(f"Found {len(jobs)} jobs")
        return jobs

    # ── Page Watchdog ─────────────────────────────────────────
    async def _ensure_page_alive(self) -> bool:
        try:
            await self.page.evaluate("1")
            return True
        except Exception:
            logger.warning("Page died — attempting tab recovery...")
            try:
                self.page = await self.context.new_page()
                await self.context.add_init_script(STEALTH_JS)
                logger.info("Recovered: new tab opened.")
                return True
            except Exception as e:
                logger.error(f"Context also dead: {e}")
                return False

    # ── Apply ─────────────────────────────────────────────────
    async def _apply_to_job(self, job: dict):
        try:
            if not await self._ensure_page_alive():
                raise RuntimeError("Page unrecoverable")

            logger.info(f"Applying: {job['title']}")

            # Click the job's own link on the listing page instead of
            # jumping straight to its URL — visible, human-shaped
            # navigation. Falls back to goto() if the listing isn't the
            # current page (or the link isn't there any more).
            link = self.page.locator(
                f'a[data-testid="job-search-job-detail-link"][href="{job["url"]}"]'
            ).first
            clicked_link = False
            try:
                if await link.is_visible(timeout=2_000):
                    await link.scroll_into_view_if_needed(timeout=2_000)
                    box = await link.bounding_box()
                    if box:
                        await bezier_mouse_move(
                            self.page,
                            int(box["x"] + box["width"] / 2 + random.randint(-5, 5)),
                            int(box["y"] + box["height"] / 2 + random.randint(-3, 3)),
                        )
                    await micro_delay()
                    await link.click()
                    await self.page.wait_for_load_state("domcontentloaded", timeout=45_000)
                    clicked_link = True
            except Exception:
                clicked_link = False

            if not clicked_link:
                try:
                    await self.page.goto(
                        job["url"], wait_until="domcontentloaded", timeout=45_000
                    )
                except Exception:
                    await asyncio.sleep(2)

            await self._recover_from_app_error()
            await short_delay()
            await scroll_to_read(self.page)

            # Re-login if job page redirected to login
            await self._ensure_logged_in()

            # ── Contract subtype gating (W2 vs C2C) ──────────────
            # Dice's search API has no structured W2/C2C field, so this
            # is resolved per-job from the detail-page text. Only
            # relevant when the user restricted themselves to exactly
            # one contract subtype.
            wants_w2  = "contract_w2" in cfg.JOB_TYPES
            wants_c2c = "contract_c2c" in cfg.JOB_TYPES
            if wants_w2 != wants_c2c:  # exactly one contract subtype selected
                page_text = await self.page.evaluate(
                    "() => document.body?.innerText || ''"
                )
                detected = classify_contract_subtype(page_text)
                if detected == "c2c" and not wants_c2c:
                    self.tracker.log(
                        job["title"], job["company"], job["location"],
                        job["url"], "skipped",
                        "Corp-to-Corp job, but only W2 selected",
                    )
                    return
                if detected == "w2" and not wants_w2:
                    self.tracker.log(
                        job["title"], job["company"], job["location"],
                        job["url"], "skipped",
                        "W2 job, but only Corp-to-Corp selected",
                    )
                    return
                # detected is None (ambiguous/undetectable) — proceed
                # best-effort rather than blocking a potentially valid job.

            # Check for block BEFORE clicking anything
            if await self._detect_block():
                raise BotBlockedError("CAPTCHA or block detected")

            # Find Apply button
            apply_btn = None
            for sel in [
                'a[data-testid="apply-button"]',
                'button[data-testid="apply-button"]',
                'a:has-text("Easy Apply")',
                'button:has-text("Easy Apply")',
                'a:has-text("Apply")',
            ]:
                loc = self.page.locator(sel).first
                try:
                    if await loc.is_visible(timeout=1_500):
                        apply_btn = loc
                        break
                except Exception:
                    continue

            if not apply_btn:
                self.tracker.log(
                    job["title"], job["company"], job["location"],
                    job["url"], "skipped", "No Apply button"
                )
                return

            # Skip if button is disabled — means already applied on Dice
            # Check via JS for instant response, no timeout needed
            is_disabled = await self.page.evaluate("""() => {
                const btn = document.querySelector('[data-testid="apply-button"]');
                return btn ? btn.disabled || btn.getAttribute('data-disabled') === 'true' : false;
            }""")
            if is_disabled:
                self.tracker.log(
                    job["title"], job["company"], job["location"],
                    job["url"], "skipped", "Already applied (button disabled)"
                )
                return

            # Bezier click
            box = await apply_btn.bounding_box()
            if box:
                await bezier_mouse_move(
                    self.page,
                    int(box["x"] + box["width"] / 2 + random.randint(-5, 5)),
                    int(box["y"] + box["height"] / 2 + random.randint(-3, 3)),
                )
                await micro_delay()

            await apply_btn.click()
            await asyncio.sleep(random.uniform(1.5, 3))

            submitted = await self._handle_apply_form()

            if submitted:
                self.tracker.log(
                    job["title"], job["company"], job["location"],
                    job["url"], "applied"
                )
                self.consecutive_fails = 0
            else:
                self.consecutive_fails += 1
                logger.error(f"Not confirmed submitted: {job['title']}")
                self.tracker.log(
                    job["title"], job["company"], job["location"],
                    job["url"], "failed", "Form did not confirm submission"
                )

        except BotBlockedError:
            raise  # bubble up to session level — triggers full restart

        except Exception as e:
            self.consecutive_fails += 1
            logger.error(f"Failed: {job['title']} — {e}")
            self.tracker.log(
                job["title"], job["company"], job["location"],
                job["url"], "failed", str(e)[:200]
            )

        finally:
            # Back to the listing so the next job's link click has
            # something to find. go_back() first (cheapest, visible,
            # matches a human hitting the browser back button); if that
            # doesn't land back on a job list, re-run the last search.
            try:
                await self.page.go_back(wait_until="domcontentloaded", timeout=15_000)
                await self.page.wait_for_selector(
                    'a[data-testid="job-search-job-detail-link"]', timeout=5_000
                )
            except Exception:
                if self._last_search_url:
                    try:
                        await self.page.goto(
                            self._last_search_url,
                            wait_until="domcontentloaded",
                            timeout=45_000,
                        )
                        await self._recover_from_app_error()
                    except Exception:
                        pass

    # ── Form Handler ──────────────────────────────────────────
    SUBMIT_SELECTORS = [
        'button:has-text("Submit Application")',
        'button:has-text("Submit")',
        'a:has-text("Submit Application")',
        'a:has-text("Submit")',
    ]
    NEXT_SELECTORS = [
        'button:has-text("Next")',
        'button:has-text("Continue")',
        'a:has-text("Next")',
        'a:has-text("Continue")',
    ]

    async def _click_submit_if_present(self) -> bool:
        """Find, scroll to, and click a Submit button if visible. Returns
        True only if a click was actually performed."""
        for sel in self.SUBMIT_SELECTORS:
            btn = self.page.locator(sel).first
            try:
                await btn.scroll_into_view_if_needed(timeout=2_000)
            except Exception:
                pass
            try:
                if await btn.is_visible(timeout=2_500):
                    box = await btn.bounding_box()
                    if box:
                        await bezier_mouse_move(
                            self.page,
                            int(box["x"] + box["width"] / 2),
                            int(box["y"] + box["height"] / 2),
                        )
                    await micro_delay()
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

    CONFIRM_SIGNALS = [
        "application submitted", "successfully applied",
        "thank you for applying", "application received",
        "you've applied", "you have applied", "application sent",
        "your application has been submitted", "applied to this job",
    ]
    ERROR_SIGNALS = [
        "required field", "is required", "please enter", "please select",
        "invalid", "error occurred", "something went wrong",
    ]

    async def _confirm_submitted(self, attempts: int = 4, interval: float = 1.5) -> bool:
        """Best-effort check that an application was actually submitted.

        Polls repeatedly instead of checking once — the confirmation
        screen/redirect can take a couple seconds to render, and a single
        early check was the main cause of real submissions being logged
        as "failed" (which then rotated the keyword unnecessarily).
        """
        for i in range(attempts):
            try:
                body = await self.page.evaluate(
                    "() => document.body?.innerText?.toLowerCase() || ''"
                )
            except Exception:
                body = ""

            if any(signal in body for signal in self.CONFIRM_SIGNALS):
                return True

            submit_visible = False
            for sel in self.SUBMIT_SELECTORS:
                try:
                    if await self.page.locator(sel).first.is_visible(timeout=500):
                        submit_visible = True
                        break
                except Exception:
                    continue

            has_error = any(signal in body for signal in self.ERROR_SIGNALS)

            if not submit_visible and not has_error:
                # Submit button gone and no validation error showing —
                # form closed/advanced, treat as submitted.
                return True

            if i < attempts - 1:
                await asyncio.sleep(interval)

        return False

    # ── Screening Question Auto-Answer ────────────────────────
    # Matches EEO/screening question text against cfg.EEO_ANSWERS and
    # picks the matching radio/checkbox option or select value. Runs
    # entirely in-page (one evaluate call) since these forms vary too
    # much in structure for reliable per-field Playwright locators.
    SCREENING_JS = """(answers) => {
        function normalize(s) { return (s || '').toLowerCase().trim(); }

        function questionTextFor(el) {
            const fieldset = el.closest('fieldset');
            if (fieldset) {
                const legend = fieldset.querySelector('legend');
                if (legend && legend.innerText.trim()) return legend.innerText;
            }
            const group = el.closest('[role="radiogroup"], [role="group"]');
            if (group) {
                const labelledBy = group.getAttribute('aria-labelledby');
                if (labelledBy) {
                    const lbl = document.getElementById(labelledBy);
                    if (lbl && lbl.innerText.trim()) return lbl.innerText;
                }
            }
            let node = el.closest('div, li');
            for (let i = 0; i < 4 && node; i++) {
                const label = node.querySelector(':scope > label, :scope > legend, :scope > p, :scope > span');
                if (label && label.innerText && label.innerText.trim().length > 3) {
                    return label.innerText;
                }
                node = node.parentElement;
            }
            return '';
        }

        function labelTextFor(input) {
            if (input.id) {
                const lbl = document.querySelector(`label[for="${input.id}"]`);
                if (lbl) return lbl.innerText;
            }
            const parentLabel = input.closest('label');
            if (parentLabel) return parentLabel.innerText;
            return input.value || '';
        }

        function matchRule(questionText) {
            const t = normalize(questionText);
            if (!t) return null;
            if (/veteran/.test(t)) return { key: 'veteran', kind: 'yesno' };
            if (/disab/.test(t)) return { key: 'disability', kind: 'yesno' };
            if (/race|ethnicit/.test(t)) return { key: 'race', kind: 'text' };
            if (/onsite|on-site|in-person interview|willing to (work|interview)/.test(t)) {
                return { key: 'onsite_interview', kind: 'yesno' };
            }
            return null;
        }

        let answered = 0;
        const seenGroups = new Set();

        // Radio groups
        const radios = Array.from(document.querySelectorAll('input[type="radio"]'));
        const groups = {};
        for (const r of radios) {
            const name = r.name || 'unnamed';
            (groups[name] = groups[name] || []).push(r);
        }
        for (const [name, inputs] of Object.entries(groups)) {
            if (seenGroups.has(name)) continue;
            const questionText = questionTextFor(inputs[0]);
            const rule = matchRule(questionText);
            if (!rule) continue;
            const desired = answers[rule.key];
            let target = null;
            for (const input of inputs) {
                const lbl = normalize(labelTextFor(input));
                if (rule.kind === 'yesno') {
                    if (desired === 'yes' && /^yes\\b/.test(lbl)) { target = input; break; }
                    if (desired === 'no' && /^no\\b/.test(lbl)) { target = input; break; }
                } else {
                    if (lbl.includes(normalize(desired))) { target = input; break; }
                }
            }
            if (target && !target.checked) {
                target.click();
                answered++;
            }
            seenGroups.add(name);
        }

        // Standalone checkboxes phrased as a yes/no question
        const checkboxes = Array.from(document.querySelectorAll('input[type="checkbox"]'));
        for (const cb of checkboxes) {
            const questionText = questionTextFor(cb) || labelTextFor(cb);
            const rule = matchRule(questionText);
            if (!rule || rule.kind !== 'yesno') continue;
            const desired = answers[rule.key] === 'yes';
            if (cb.checked !== desired) {
                cb.click();
                answered++;
            }
        }

        // Select dropdowns
        const selects = Array.from(document.querySelectorAll('select'));
        for (const sel of selects) {
            const questionText = questionTextFor(sel);
            const rule = matchRule(questionText);
            if (!rule) continue;
            const desired = rule.kind === 'yesno'
                ? (answers[rule.key] === 'yes' ? 'yes' : 'no')
                : normalize(answers[rule.key]);
            let match = null;
            for (const opt of sel.options) {
                if (normalize(opt.text).includes(desired)) { match = opt; break; }
            }
            if (match && sel.value !== match.value) {
                sel.value = match.value;
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                answered++;
            }
        }

        return answered;
    }"""

    async def _answer_screening_questions(self):
        try:
            answered = await self.page.evaluate(self.SCREENING_JS, dict(cfg.EEO_ANSWERS))
            if answered:
                logger.info(f"Auto-answered {answered} screening question(s)")
                await micro_delay()
        except Exception as e:
            logger.debug(f"Screening auto-answer skipped: {e}")

    async def _handle_apply_form(self) -> bool:
        await asyncio.sleep(1.5)

        # Walk through up to 8 form steps
        for _ in range(8):
            await self._answer_screening_questions()

            if await self._click_submit_if_present():
                if await self._confirm_submitted():
                    logger.info("Submitted.")
                    return True
                # Click landed but nothing confirms it (e.g. validation
                # error blocked it) — retry once before giving up on this step.
                await asyncio.sleep(1)
                if await self._click_submit_if_present():
                    if await self._confirm_submitted():
                        logger.info("Submitted (after retry).")
                        return True
                logger.warning("Submit clicked but not confirmed — continuing form walk.")

            # Try Next / Continue
            found_next = False
            for sel in self.NEXT_SELECTORS:
                btn = self.page.locator(sel).first
                try:
                    if await btn.is_visible(timeout=1_000):
                        await btn.click()
                        await short_delay()
                        found_next = True
                        break
                except Exception:
                    continue

            if not found_next:
                break

        # Final fallback — one more patient attempt before giving up
        await asyncio.sleep(1.5)
        if await self._click_submit_if_present():
            await asyncio.sleep(2)
            if await self._confirm_submitted():
                logger.info("Submitted (final fallback).")
                return True

        return False

    # ── App Error Recovery ──────────────────────────────────────
    async def _recover_from_app_error(self) -> bool:
        """Detect Dice's Next.js error-boundary page ('Something went
        wrong' / Digest ID) and reload once to clear it.

        Returns True if the error page was hit (whether or not the
        reload recovered it) so callers can back off / retry.
        """
        try:
            body = await self.page.evaluate(
                "() => document.body?.innerText?.toLowerCase() || ''"
            )
        except Exception:
            return False

        if "something went wrong" in body and "digest id" in body:
            logger.warning("Dice app error page hit — reloading")
            try:
                snippet = body[:600].replace("\n", " | ")
                logger.warning(
                    f"DIAGNOSTIC url={self.page.url} status_snippet='{snippet}'"
                )
                await self.page.screenshot(path="error_diagnostic.png")
            except Exception:
                pass
            try:
                await self.page.reload(wait_until="domcontentloaded", timeout=45_000)
                await short_delay()
            except Exception as e:
                logger.warning(f"Reload after app error failed: {e}")
            return True
        return False

    # ── Block Detection ───────────────────────────────────────
    async def _detect_block(self) -> bool:
        try:
            for sel in [
                'iframe[src*="captcha"]',
                'iframe[src*="recaptcha"]',
                'iframe[src*="hcaptcha"]',
                '#captcha-container',
                '.captcha-wrapper',
                '[data-testid="captcha"]',
            ]:
                try:
                    if await self.page.locator(sel).first.is_visible(timeout=800):
                        return True
                except Exception:
                    continue

            title = (await self.page.title()).lower()
            for signal in ["access denied", "403", "blocked", "too many requests", "429"]:
                if signal in title:
                    return True

            # Also check page body for block signals
            body = await self.page.evaluate("() => document.body?.innerText?.toLowerCase() || ''")
            for signal in ["you have been blocked", "unusual traffic", "verify you are human"]:
                if signal in body:
                    return True

        except Exception:
            pass
        return False

    # ── Sleep Schedule ────────────────────────────────────────
    def _is_sleep_time(self) -> bool:
        hour = datetime.now().hour
        return cfg.SLEEP_HOUR_START <= hour < cfg.SLEEP_HOUR_END

    async def _sleep_until_wake(self):
        now = datetime.now()
        wake_time = now.replace(
            hour=cfg.SLEEP_HOUR_END, minute=0, second=0, microsecond=0
        )
        if wake_time <= now:
            wake_time += timedelta(days=1)
        remaining = (wake_time - now).total_seconds()

        await countdown_sleep(
            remaining,
            label=f"Sleep schedule ({cfg.SLEEP_HOUR_START}:00-{cfg.SLEEP_HOUR_END}:00) — waking",
        )
        logger.info("Woke up. Resuming...")


# ── Custom Exceptions ─────────────────────────────────────────
class BotBlockedError(Exception):
    pass


# ── Entry Point ───────────────────────────────────────────────
async def main():
    bot = DiceBot()
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
