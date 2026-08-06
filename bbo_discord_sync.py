#!/usr/bin/env python3
"""Collect Wei1011's BBO hands and publish grouped LIN links to Discord."""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as midnight
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests

MYHANDS_URL = "https://www.bridgebase.com/myhands/index.php?&from_login=1"
DEFAULT_TARGET = "wei1011"
DEFAULT_ZONE = "Asia/Taipei"


@dataclass(frozen=True)
class Hand:
    timestamp: int
    url: str
    label: str = ""
    screenshot: str = ""


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--target-user", default=os.getenv("BBO_TARGET_USER", DEFAULT_TARGET))
    p.add_argument("--state", type=Path, default=Path("state.json"))
    p.add_argument("--timezone", default=os.getenv("BBO_TIMEZONE", DEFAULT_ZONE))
    p.add_argument("--days", type=int, default=20)
    p.add_argument("--group-minutes", type=int, default=30)
    p.add_argument("--manual-login", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def canonical(url: str) -> str | None:
    p = urlparse(html_lib.unescape(url).replace("\\/", "/"))
    if "lin=" not in p.query and "myhand=" not in p.query:
        return None
    if "lin=" in p.query:
        q = p.query if "bbo=" in p.query else "bbo=y&" + p.query
        return "https://www.bridgebase.com/tools/handviewer.html?" + q
    return url


def timestamp_from_url(url: str) -> int | None:
    values = [int(x) for x in re.findall(r"(?<!\d)(1[5-9]\d{8}|2\d{9})(?!\d)", url)]
    return min(values) if values else None


def timestamp_from_text(text: str, zone: str) -> int | None:
    patterns = [
        r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})[ T]+(\d{1,2}):(\d{2})",
        r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2})[ T]+(\d{1,2}):(\d{2})",
    ]
    for i, pattern in enumerate(patterns):
        m = re.search(pattern, text)
        if not m:
            continue
        a = list(map(int, m.groups()))
        if i == 0:
            year, month, day, hour, minute = a
        else:
            month, day, year, hour, minute = a
        return int(datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(zone)).timestamp())
    return None


def row_text(anchor) -> str:
    try:
        return " ".join(anchor.locator("xpath=ancestor::tr[1]").inner_text(timeout=2_000).split())
    except Exception:
        return " ".join((anchor.inner_text() or "").split())


def links_on_page(page) -> list[tuple[str, str]]:
    links = []
    for anchor in page.locator("a").all():
        href = anchor.get_attribute("href") or ""
        url = canonical(urljoin(page.url, href))
        if url:
            links.append((url, row_text(anchor)))
    return links


def extract_lin_url(page, fallback: str) -> str | None:
    candidates = [page.url]
    candidates += [url for url, _ in links_on_page(page)]
    source = html_lib.unescape(page.content()).replace("\\/", "/")
    candidates += re.findall(r"(?:https?://|/)[^\"'<> ]*handviewer\.html\?[^\"'<> ]*lin=[^\"'<> ]+", source)
    for candidate in candidates:
        value = canonical(candidate)
        if value and "lin=" in value:
            return value
    return canonical(fallback) if "lin=" in fallback else None


def players_from_lin(url: str) -> set[str]:
    raw = parse_qs(urlparse(url).query).get("lin", [""])[0]
    match = re.search(r"(?:^|\|)pn\|([^|]*)", unquote(raw), re.I)
    return {x.strip().lower() for x in match.group(1).split(",")} if match else set()


def board_label(url: str, index: int) -> str:
    raw = unquote(parse_qs(urlparse(url).query).get("lin", [""])[0])
    m = re.search(r"(?:^|\|)ah\|Board\s+([^|]+)", raw, re.I)
    return f"Board {m.group(1).strip()}" if m else f"Hand {index}"


def collect_hands(page, target: str, zone: str, screenshot_dir: Path) -> list[Hand]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    candidates: dict[str, str] = {}
    for url, text in links_on_page(page):
        candidates[url] = text
    travellers: dict[str, str] = {}
    for anchor in page.locator("a").all():
        href = anchor.get_attribute("href") or ""
        if "traveller=" in href:
            travellers[urljoin(page.url, href)] = row_text(anchor)
    for traveller, text in travellers.items():
        try:
            page.goto(traveller, wait_until="domcontentloaded", timeout=45_000)
            for url, link_text in links_on_page(page):
                candidates[url] = link_text or text
        except PlaywrightTimeoutError:
            print(f"traveller timeout: {traveller}", file=sys.stderr)

    result: list[Hand] = []
    seen: set[str] = set()
    for index, (candidate, text) in enumerate(candidates.items(), 1):
        try:
            page.goto(candidate, wait_until="networkidle", timeout=45_000)
            page.wait_for_timeout(300)
        except PlaywrightTimeoutError:
            print(f"hand timeout: {candidate}", file=sys.stderr)
            continue
        lin_url = extract_lin_url(page, candidate)
        if not lin_url or lin_url in seen:
            continue
        seen.add(lin_url)
        if target.lower() not in players_from_lin(lin_url):
            continue
        timestamp = timestamp_from_url(candidate) or timestamp_from_url(lin_url) or timestamp_from_text(text, zone)
        if timestamp is None:
            continue
        filename = f"{timestamp}-{hashlib.sha1(lin_url.encode()).hexdigest()[:10]}.png"
        screenshot = screenshot_dir / filename
        page.screenshot(path=str(screenshot), full_page=True)
        result.append(Hand(timestamp, lin_url, board_label(lin_url, index), str(screenshot)))
    return sorted(result, key=lambda h: (h.timestamp, h.url))


def groups(hands: list[Hand], gap: int) -> list[list[Hand]]:
    output: list[list[Hand]] = []
    for hand in hands:
        if not output or hand.timestamp - output[-1][-1].timestamp > gap:
            output.append([hand])
        else:
            output[-1].append(hand)
    return output


def stable_groups(hands: list[Hand], now: int, gap: int) -> tuple[list[list[Hand]], list[Hand]]:
    batches = groups(hands, gap)
    if batches and now - batches[-1][-1].timestamp < gap:
        return batches[:-1], batches[-1]
    return batches, []


def group_stable_hands(hands: list[Hand], now: int, gap_seconds: int = 30 * 60) -> tuple[list[Hand], list[Hand]]:
    """Backward-compatible flat view used by the unit tests."""
    batches, pending = stable_groups(hands, now, gap_seconds)
    return [hand for batch in batches for hand in batch], pending


def discord_request(method: str, url: str, token: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bot {token}"
    for _ in range(8):
        response = requests.request(method, url, headers=headers, timeout=60, **kwargs)
        if response.status_code != 429:
            response.raise_for_status()
            return response
        try:
            delay = float(response.json().get("retry_after", 2))
        except (ValueError, TypeError):
            delay = 2
        print(f"Discord 限流，等待 {delay + 0.5:.1f} 秒…", file=sys.stderr)
        time.sleep(delay + 0.5)
    raise RuntimeError("Discord 持續限流，已停止。")


def create_thread(channel_id: str, token: str, name: str) -> str:
    root = discord_request("POST", f"https://discord.com/api/v10/channels/{channel_id}/messages", token, json={"content": f"BBO 牌局批次：{name}"}).json()
    thread = discord_request("POST", f"https://discord.com/api/v10/channels/{channel_id}/messages/{root['id']}/threads", token, json={"name": name}).json()
    return thread["id"]


def send_hand(thread_id: str, token: str, hand: Hand, zone: str) -> None:
    when = datetime.fromtimestamp(hand.timestamp, ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M")
    payload = {"content": f"{hand.label}｜{when}\n{hand.url}"}
    with open(hand.screenshot, "rb") as image:
        discord_request("POST", f"https://discord.com/api/v10/channels/{thread_id}/messages", token, files={"files[0]": (Path(hand.screenshot).name, image, "image/png")}, data={"payload_json": json.dumps(payload)})


def read_state(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {"last_stable_time": 0, "sent_urls": [], "threads": {}}


def main() -> int:
    cfg = parse_args()
    now = int(time.time())
    state = read_state(cfg.state)
    start = state.get("last_stable_time") or now - cfg.days * 86400
    username, password = os.getenv("BBO_USERNAME"), os.getenv("BBO_PASSWORD")
    token, channel_id = os.getenv("DISCORD_BOT_TOKEN"), os.getenv("DISCORD_CHANNEL_ID")
    if not cfg.manual_login and (not username or not password):
        raise SystemExit("缺少 BBO_USERNAME 或 BBO_PASSWORD")
    if not cfg.dry_run and (not token or not channel_id):
        raise SystemExit("建立討論串需要 DISCORD_BOT_TOKEN 與 DISCORD_CHANNEL_ID")
    screenshot_dir = Path("screenshots")
    screenshot_dir.mkdir(exist_ok=True)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not cfg.manual_login)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-TW")
        page = context.new_page()
        try:
            page.goto(MYHANDS_URL, wait_until="domcontentloaded", timeout=60_000)
            if page.locator('input[type="password"]').count():
                if cfg.manual_login:
                    input("請手動登入 BBO 後按 Enter：")
                else:
                    page.locator('input[name="username"], input[name="user"], input[type="text"]').first.fill(username)
                    page.locator('input[type="password"]').first.fill(password)
                    page.locator('button[type="submit"], input[type="submit"]').first.click()
                    page.wait_for_timeout(2_000)
            form = page.locator("#myhands")
            if not form.count():
                page.goto(MYHANDS_URL, wait_until="domcontentloaded", timeout=60_000)
                form = page.locator("#myhands")
            today = datetime.now(ZoneInfo(cfg.timezone)).date()
            form.locator('input[name="username"]').fill(cfg.target_user)
            form.locator('select[name="start_time[Y]"]').select_option(str(today.year))
            form.locator('select[name="start_time[M]"]').select_option(str(today.month))
            form.locator('select[name="start_time[d]"]').select_option(str(today.day))
            form.locator('select[name="time_interval[0]"]').select_option("1")
            form.locator('select[name="time_interval[1]"]').select_option("3")
            form.locator('input[name="time_arrow"][value="-"]').check()
            form.locator('select[name="summaries[0]"]').select_option("3")
            form.locator('input[type="submit"]').first.click()
            page.wait_for_load_state("domcontentloaded")
            hands = [h for h in collect_hands(page, cfg.target_user, cfg.timezone, screenshot_dir) if h.timestamp >= start]
        finally:
            context.close()
            browser.close()

    stable, pending = stable_groups(hands, now, cfg.group_minutes * 60)
    print(f"查到 {len(hands)} 筆；穩定 {sum(map(len, stable))} 筆；待下次 {len(pending)} 筆")
    sent = set(state.get("sent_urls", []))
    thread_ids = state.setdefault("threads", {})
    for batch in stable:
        new = [h for h in batch if h.url not in sent]
        if not new:
            continue
        key = str(batch[0].timestamp)
        if not cfg.dry_run:
            thread_id = thread_ids.get(key) or create_thread(channel_id, token, datetime.fromtimestamp(batch[0].timestamp, ZoneInfo(cfg.timezone)).strftime("%Y-%m-%d %H:%M"))
            thread_ids[key] = thread_id
            for hand in new:
                send_hand(thread_id, token, hand, cfg.timezone)
                sent.add(hand.url)
                state["sent_urls"] = list(sent)[-5000:]
                cfg.state.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        else:
            sent.update(h.url for h in new)
    if not cfg.dry_run:
        state["sent_urls"] = list(sent)[-5000:]
        if stable and not pending:
            state["last_stable_time"] = max(h.timestamp for batch in stable for h in batch)
        cfg.state.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
