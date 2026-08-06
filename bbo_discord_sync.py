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
    p.add_argument("--lookback-hours", type=float, default=24)
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


def timestamp_from_text(text: str) -> int | None:
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
        return int(datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(DEFAULT_TIMEZONE)).timestamp())
    return None


def timestamp_from_url(url: str) -> int | None:
    # BBO's myhand/traveller identifiers commonly contain a Unix timestamp.
    candidates = [int(value) for value in re.findall(r"(?<!\d)(1[5-9]\d{8}|2\d{9})(?!\d)", url)]
    return min(candidates) if candidates else None


def extract_timestamp(url: str, row_text: str) -> int | None:
    return timestamp_from_url(url) or timestamp_from_text(row_text)


def fetch_hands(page, target_url: str) -> list[Hand]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(1_000)
    found: dict[str, Hand] = {}
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
        timestamp = extract_timestamp(url, row_text)
        if timestamp is not None:
            found[url] = Hand(timestamp=timestamp, url=url, label=" ".join(row_text.split()))
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


def send_discord(webhook: str, hands: list[Hand], timezone: str) -> None:
    if not hands:
        return
    zone = ZoneInfo(timezone)
    for hand in hands:
        when = datetime.fromtimestamp(hand.timestamp, zone).strftime("%Y-%m-%d %H:%M")
        content = f"BBO 新牌局：{when} ({timezone})\n{hand.url}"
        response = requests.post(webhook, json={"content": content}, timeout=30)
        response.raise_for_status()
        time.sleep(1.0)


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

    target_url = make_query(cfg.target_user, start, now)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not cfg.manual_login)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-TW")
        page = context.new_page()
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
            if "/myhands/index.php" in page.url:
                if cfg.manual_login:
                    input("請在瀏覽器手動登入 BBO，完成後按 Enter：")
                else:
                    user = page.locator('input[name="username"], input[name="user"], input[type="text"]').first
                    secret = page.locator('input[type="password"]').first
                    user.fill(username)
                    secret.fill(password)
                    page.locator('button[type="submit"], input[type="submit"]').first.click()
                    page.wait_for_load_state("domcontentloaded")
            hands = fetch_hands(page, target_url)
        finally:
            context.close()
            browser.close()

    stable, pending = group_stable_hands(hands, now, cfg.group_minutes * 60)
    sent = set(state.get("sent_urls", []))
    new_hands = [hand for hand in stable if hand.url not in sent and hand.timestamp >= start]
    print(f"查到 {len(hands)} 筆；穩定 {len(stable)} 筆；待下次 {len(pending)} 筆；新增 {len(new_hands)} 筆")
    for hand in pending:
        print(f"PENDING {hand.timestamp}: {hand.url}")
    if new_hands and not cfg.dry_run:
        send_discord(webhook, new_hands, cfg.timezone)
    if not cfg.dry_run:
        state["sent_urls"] = list((sent | {hand.url for hand in new_hands}))[-5000:]
        if stable:
            state["last_stable_time"] = max(hand.timestamp for hand in stable)
        write_state(cfg.state, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
