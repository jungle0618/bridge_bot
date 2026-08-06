#!/usr/bin/env python3
"""Sync stable BBO hand records to a Discord webhook.

Credentials are read from environment variables. The last stable timestamp
and already sent URLs are stored in state.json so Actions can run repeatedly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, time as midnight
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests


MYHANDS_URL = "https://www.bridgebase.com/myhands/hands.php"
LOGIN_URL = "https://www.bridgebase.com/myhands/index.php?&from_login=1"
DEFAULT_TARGET = "wei1011"
DEFAULT_TIMEZONE = "Asia/Taipei"
DEFAULT_GAP = 30 * 60


@dataclass(frozen=True)
class Hand:
    timestamp: int
    url: str
    label: str = ""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync BBO hands to Discord")
    p.add_argument("--target-user", default=os.getenv("BBO_TARGET_USER", DEFAULT_TARGET))
    p.add_argument("--state", type=Path, default=Path("state.json"))
    p.add_argument("--timezone", default=os.getenv("BBO_TIMEZONE", DEFAULT_TIMEZONE))
    p.add_argument("--lookback-hours", type=float, default=20 * 24, help="首次執行查詢的回溯時間，預設 20 天")
    p.add_argument("--group-minutes", type=int, default=30)
    p.add_argument("--manual-login", action="store_true", help="Use visible browser and login manually")
    p.add_argument("--dry-run", action="store_true", help="Do not send Discord messages or write state")
    return p.parse_args()


def date_timestamp(value: str, timezone: str) -> int:
    zone = ZoneInfo(timezone)
    return int(datetime.combine(date.fromisoformat(value), midnight.min, tzinfo=zone).timestamp())


def make_query(target: str, start: int, end: int) -> str:
    return MYHANDS_URL + "?" + urlencode({"username": target, "start_time": start, "end_time": end})


def canonical_hand_url(url: str) -> str | None:
    parsed = urlparse(url)
    if "lin=" not in parsed.query and "myhand=" not in parsed.query:
        return None
    if "lin=" in parsed.query:
        query = parsed.query
        if "bbo=" not in query:
            query = "bbo=y&" + query
        return "https://www.bridgebase.com/tools/handviewer.html?" + query
    return url


def timestamp_from_text(text: str, timezone: str = DEFAULT_TIMEZONE) -> int | None:
    patterns = [
        r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})[ T]+(\d{1,2}):(\d{2})",
        r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2})[ T]+(\d{1,2}):(\d{2})",
        r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(20\d{2})[ ,]+(\d{1,2}):(\d{2})",
    ]
    months = {name: number for number, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )}
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        values = match.groups()
        if index == 0:
            year, month, day, hour, minute = map(int, values)
        elif index == 1:
            month, day, year, hour, minute = map(int, values)
        else:
            day, month_name, year, hour, minute = values
            month, year, day, hour, minute = months[month_name[:3].lower()], int(year), int(day), int(hour), int(minute)
        return int(datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(timezone)).timestamp())
    return None


def timestamp_from_url(url: str) -> int | None:
    # BBO's myhand/traveller identifiers commonly contain a Unix timestamp.
    candidates = [int(value) for value in re.findall(r"(?<!\d)(1[5-9]\d{8}|2\d{9})(?!\d)", url)]
    return min(candidates) if candidates else None


def extract_timestamp(url: str, row_text: str, timezone: str = DEFAULT_TIMEZONE) -> int | None:
    return timestamp_from_url(url) or timestamp_from_text(row_text, timezone)


def viewer_links_on_page(page) -> list[tuple[str, str]]:
    """Return (viewer_url, nearby_text) links from the current BBO page."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    links: list[tuple[str, str]] = []
    for anchor in page.locator("a").all():
        href = anchor.get_attribute("href") or ""
        if "handviewer.html" not in href and "lin=" not in href and "myhand=" not in href:
            continue
        url = canonical_hand_url(urljoin(page.url, href))
        if not url:
            continue
        try:
            row_text = anchor.locator("xpath=ancestor::tr[1]").inner_text(timeout=2_000)
        except PlaywrightTimeoutError:
            row_text = anchor.inner_text()
        links.append((url, " ".join(row_text.split())))
    return links


def fetch_hands(page, timezone: str = DEFAULT_TIMEZONE) -> list[Hand]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    page.wait_for_timeout(1_000)
    found: dict[str, Hand] = {}
    traveller_links: list[tuple[str, str]] = []
    for anchor in page.locator("a").all():
        href = anchor.get_attribute("href") or ""
        if "traveller=" not in href:
            continue
        try:
            row_text = anchor.locator("xpath=ancestor::tr[1]").inner_text(timeout=2_000)
        except PlaywrightTimeoutError:
            row_text = anchor.inner_text()
        traveller_links.append((urljoin(page.url, href), " ".join(row_text.split())))

    for url, row_text in viewer_links_on_page(page):
        timestamp = extract_timestamp(url, row_text, timezone)
        if timestamp is not None:
            found[url] = Hand(timestamp=timestamp, url=url, label=row_text)

    # My Hands search results normally expose traveller URLs first. Each
    # traveller page contains the Movie link that expands to a handviewer URL.
    seen_travellers: set[str] = set()
    for traveller_url, result_text in traveller_links:
        if traveller_url in seen_travellers:
            continue
        seen_travellers.add(traveller_url)
        try:
            page.goto(traveller_url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(300)
            for viewer_url, movie_text in viewer_links_on_page(page):
                timestamp = extract_timestamp(viewer_url, movie_text, timezone) or timestamp_from_url(traveller_url)
                if timestamp is not None:
                    found[viewer_url] = Hand(timestamp=timestamp, url=viewer_url, label=movie_text or result_text)
        except PlaywrightTimeoutError:
            print(f"跳過逾時 traveller：{traveller_url}", file=sys.stderr)
    return sorted(found.values(), key=lambda item: (item.timestamp, item.url))


def group_stable_hands(hands: list[Hand], now: int, gap_seconds: int = DEFAULT_GAP) -> tuple[list[Hand], list[Hand]]:
    """Return (stable, pending). The newest close-together group stays pending."""
    if not hands:
        return [], []
    groups: list[list[Hand]] = [[hands[0]]]
    for hand in hands[1:]:
        if hand.timestamp - groups[-1][-1].timestamp <= gap_seconds:
            groups[-1].append(hand)
        else:
            groups.append([hand])
    if now - groups[-1][-1].timestamp < gap_seconds:
        return [hand for group in groups[:-1] for hand in group], groups[-1]
    return [hand for group in groups for hand in group], []


def read_state(path: Path) -> dict:
    if not path.exists():
        return {"last_stable_time": 0, "sent_urls": []}
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send_discord(webhook: str, hands: list[Hand], timezone: str) -> list[Hand]:
    if not hands:
        return []
    zone = ZoneInfo(timezone)
    delivered: list[Hand] = []
    for hand in hands:
        when = datetime.fromtimestamp(hand.timestamp, zone).strftime("%Y-%m-%d %H:%M")
        content = f"BBO 新牌局：{when} ({timezone})\n{hand.url}"
        for attempt in range(6):
            response = requests.post(webhook, json={"content": content}, timeout=30)
            if response.status_code == 429:
                try:
                    retry_after = float(response.json().get("retry_after", 2))
                except (ValueError, TypeError):
                    retry_after = float(response.headers.get("Retry-After", 2))
                wait_seconds = max(retry_after, 1.0) + 0.5
                print(f"Discord 限流，等待 {wait_seconds:.1f} 秒後重試…", file=sys.stderr)
                time.sleep(wait_seconds)
                continue
            if response.status_code == 404:
                raise RuntimeError("Discord Webhook 不存在或已失效，請重新建立並更新 DISCORD_WEBHOOK_URL。")
            response.raise_for_status()
            delivered.append(hand)
            time.sleep(0.5)
            break
        else:
            raise RuntimeError("Discord 持續回傳 429，已停止發送；下次執行會繼續未完成的牌局。")
    return delivered


def login_and_open_myhands(page, username: str | None, password: str | None, manual: bool) -> None:
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
    if page.locator('input[type="password"]').count() == 0:
        return
    if manual:
        input("請在瀏覽器手動登入 BBO，完成後按 Enter：")
        return
    user = page.locator('input[name="username"], input[name="user"], input[type="text"]').first
    secret = page.locator('input[type="password"]').first
    user.fill(username or "")
    secret.fill(password or "")
    page.locator('button[type="submit"], input[type="submit"]').first.click()
    for _ in range(30):
        page.wait_for_timeout(1_000)
        if page.locator('input[type="password"]').count() == 0:
            return
    raise RuntimeError("BBO 登入未完成，仍停留在登入頁。")


def search_by_bbo_form(page, target: str, timezone: str) -> None:
    """Use the same date/interval form as BBO's My Hands page."""
    today = datetime.now(ZoneInfo(timezone)).date()
    form = page.locator("#myhands")
    if form.count() == 0:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        form = page.locator("#myhands")
    if form.count() == 0:
        raise RuntimeError("登入後找不到 BBO My Hands 日期表單。")
    form.locator('input[name="username"]').fill(target)
    form.locator('select[name="start_time[Y]"]').select_option(str(today.year))
    form.locator('select[name="start_time[M]"]').select_option(str(today.month))
    form.locator('select[name="start_time[d]"]').select_option(str(today.day))
    # BBO's form only offers up to a few days, so use 3 weeks (21 days)
    # and apply the exact 20-day cutoff after parsing the results.
    form.locator('select[name="time_interval[0]"]').select_option("1")  # week(s)
    form.locator('select[name="time_interval[1]"]').select_option("3")
    form.locator('input[name="time_arrow"][value="-"]').check()
    form.locator('select[name="summaries[0]"]').select_option("3")
    form.locator('input[type="submit"]').first.click()
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1_000)


def main() -> int:
    cfg = parse_args()
    state = read_state(cfg.state)
    now = int(time.time())
    start = state.get("last_stable_time") or now - int(cfg.lookback_hours * 3600)
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    username = os.getenv("BBO_USERNAME")
    password = os.getenv("BBO_PASSWORD")
    if not cfg.manual_login and (not username or not password):
        raise SystemExit("請設定 BBO_USERNAME、BBO_PASSWORD；本機測試可加 --manual-login")
    if not webhook and not cfg.dry_run:
        raise SystemExit("請設定 DISCORD_WEBHOOK_URL")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not cfg.manual_login)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-TW")
        page = context.new_page()
        try:
            login_and_open_myhands(page, username, password, cfg.manual_login)
            search_by_bbo_form(page, cfg.target_user, cfg.timezone)
            hands = fetch_hands(page, cfg.timezone)
            hands = [hand for hand in hands if hand.timestamp >= start]
        finally:
            context.close()
            browser.close()

    stable, pending = group_stable_hands(hands, now, cfg.group_minutes * 60)
    sent = set(state.get("sent_urls", []))
    new_hands = [hand for hand in stable if hand.url not in sent and hand.timestamp >= start]
    print(f"查到 {len(hands)} 筆；穩定 {len(stable)} 筆；待下次 {len(pending)} 筆；新增 {len(new_hands)} 筆")
    for hand in pending:
        print(f"PENDING {hand.timestamp}: {hand.url}")
    delivered: list[Hand] = []
    if new_hands and not cfg.dry_run:
        try:
            delivered = send_discord(webhook, new_hands, cfg.timezone)
        except Exception:
            # Preserve successfully delivered URLs even when a later message
            # fails, so a retry does not resend the whole batch.
            state["sent_urls"] = list((sent | {hand.url for hand in delivered}))[-5000:]
            write_state(cfg.state, state)
            raise
    if not cfg.dry_run:
        state["sent_urls"] = list((sent | {hand.url for hand in delivered}))[-5000:]
        if stable and len(delivered) == len(new_hands):
            state["last_stable_time"] = max(hand.timestamp for hand in stable)
        write_state(cfg.state, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
