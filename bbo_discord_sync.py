#!/usr/bin/env python3
"""Collect Wei1011's BBO hands and publish grouped LIN links to Discord."""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, time as midnight
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

MYHANDS_URL = "https://www.bridgebase.com/myhands/index.php?&from_login=1"
MYHANDS_RESULTS_URL = "https://www.bridgebase.com/myhands/hands.php"
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


def lin_links_on_page(page) -> list[tuple[str, str]]:
    """Read the permanent LIN embedded in Movie's hv_popuplin onclick."""
    links: list[tuple[str, str]] = []
    pattern = re.compile(r"(?:(?:https?:)?//|/)[^\"'()<> ]*(?:handviewer\.html|hands\.php)\?[^\"'()<> ]*lin=[^\"'()<> ]+", re.I)
    for anchor in page.locator("a").all():
        text = row_text(anchor)
        attrs = [anchor.get_attribute(name) or "" for name in ("href", "onclick", "data-href", "data-url")]
        for raw in attrs:
            popup = re.search(r"hv_popuplin\(\s*['\"](.*?)['\"]\s*\)", html_lib.unescape(raw), re.I)
            if popup:
                lin = popup.group(1).replace("\\'", "'").replace('\\"', '"')
                row_hrefs = []
                try:
                    for row_anchor in anchor.locator("xpath=ancestor::tr[1]//a").all():
                        row_hrefs.append(row_anchor.get_attribute("href") or "")
                except Exception:
                    pass
                links.append(("https://www.bridgebase.com/tools/handviewer.html?bbo=y&lin=" + lin, text + " " + raw + " " + " ".join(row_hrefs)))
                continue
            matches = pattern.findall(html_lib.unescape(raw).replace("\\/", "/"))
            if not matches and "lin=" in raw:
                matches = [raw]
            for match in matches:
                value = canonical(urljoin(page.url, match))
                if value and "lin=" in value:
                    links.append((value, text))
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
    # Some versions keep the expanded LIN only as a JavaScript/string value,
    # without the handviewer URL around it.
    for match in re.finditer(r"(?:[?&]lin=|['\"]lin['\"]\s*[:=]\s*['\"])([^'\"<>]+)", source, re.I):
        value = match.group(1).replace("\\u0026", "&").replace("\\u003d", "=")
        if "pn" in unquote(value).lower() and "md" in unquote(value).lower():
            return "https://www.bridgebase.com/tools/handviewer.html?bbo=y&lin=" + value
    return canonical(fallback) if "lin=" in fallback else None


def players_from_lin(url: str) -> set[str]:
    raw = parse_qs(urlparse(url).query).get("lin", [""])[0]
    match = re.search(r"(?:^|\|)pn\|([^|]*)", unquote(raw), re.I)
    return {x.strip().lower() for x in match.group(1).split(",")} if match else set()


def rendered_players(page) -> set[str]:
    return {
        text.strip().lower()
        for text in page.locator(".nameTextDivStyle").all_inner_texts()
        if text.strip()
    }


def board_label(url: str, index: int) -> str:
    raw = unquote(parse_qs(urlparse(url).query).get("lin", [""])[0])
    m = re.search(r"(?:^|\|)ah\|Board\s+([^|]+)", raw, re.I)
    return f"Board {m.group(1).strip()}" if m else f"Hand {index}"


def collect_hands(page, target: str, zone: str, screenshot_dir: Path) -> list[Hand]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    # Important: this page is the single result page returned by the one BBO
    # form submission. Do not open traveller/myhand pages to search again.
    candidates: dict[str, str] = {
        url: text for url, text in lin_links_on_page(page)
    }
    print(f"第一次查詢結果頁找到 Lin {len(candidates)} 筆")

    result: list[Hand] = []
    seen: set[str] = set()
    for index, (candidate, text) in enumerate(candidates.items(), 1):
        # LIN already contains all four player names; discard other tables
        # before opening the URL for the screenshot.
        if "lin=" in candidate and target.lower() not in players_from_lin(candidate):
            continue
        try:
            page.goto(candidate, wait_until="networkidle", timeout=45_000)
            page.wait_for_timeout(300)
        except PlaywrightTimeoutError:
            print(f"hand timeout: {candidate}", file=sys.stderr)
            continue
        except Exception as exc:
            print(f"hand load failed: {candidate} ({exc})", file=sys.stderr)
            continue
        lin_url = extract_lin_url(page, candidate)
        # The result page's Lin URL is authoritative. Never replace it with
        # a myhand URL or try another search page.
        lin_url = candidate if "lin=" in candidate else lin_url
        if not lin_url or lin_url in seen:
            continue
        seen.add(lin_url)
        timestamp = timestamp_from_url(candidate) or timestamp_from_url(text) or timestamp_from_url(lin_url) or timestamp_from_text(text, zone)
        if timestamp is None:
            continue
        local_dt = datetime.fromtimestamp(timestamp, ZoneInfo(zone))
        day_dir = screenshot_dir / local_dt.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{local_dt.strftime('%H%M%S')}-{board_label(lin_url, index).lower().replace(' ', '-')}-{hashlib.sha1(lin_url.encode()).hexdigest()[:8]}.png"
        screenshot = day_dir / filename
        page.screenshot(path=str(screenshot), full_page=True)
        result.append(Hand(timestamp, lin_url, board_label(lin_url, index), str(screenshot)))
    print(f"展開 LIN 成功 {len(seen)} 筆；包含 {target} 的牌局 {len(result)} 筆")
    return sorted(result, key=lambda h: (h.timestamp, h.url))


def organize_batch(batch: list[Hand], screenshot_dir: Path, zone: str) -> list[Hand]:
    """Move screenshots into a date/session folder and return updated paths."""
    if not batch:
        return batch
    first = datetime.fromtimestamp(min(hand.timestamp for hand in batch), ZoneInfo(zone))
    session_dir = screenshot_dir / first.strftime("%Y-%m-%d") / f"session-{first.strftime('%H%M')}"
    session_dir.mkdir(parents=True, exist_ok=True)
    organized: list[Hand] = []
    for number, hand in enumerate(batch, 1):
        old_path = Path(hand.screenshot)
        board = hand.label.lower().replace(" ", "-")
        new_path = session_dir / f"{number:02d}-{board}-{first.strftime('%H%M')}.png"
        if old_path.exists() and old_path.resolve() != new_path.resolve():
            shutil.move(str(old_path), str(new_path))
        organized.append(replace(hand, screenshot=str(new_path)))
    return organized


def board_number(hand: Hand) -> int:
    match = re.search(r"board\s+(\d+)", hand.label, re.I)
    return int(match.group(1)) if match else 10**9


def ordered_batch(batch: list[Hand]) -> list[Hand]:
    return sorted(batch, key=lambda hand: (board_number(hand), hand.timestamp, hand.url))


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


def create_group_channel(parent_channel_id: str, token: str, name: str) -> str:
    parent = discord_request("GET", f"https://discord.com/api/v10/channels/{parent_channel_id}", token).json()
    safe_name = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:90]
    payload = {"name": f"bbo-{safe_name}", "type": 0}
    if parent.get("parent_id"):
        payload["parent_id"] = parent["parent_id"]
    channel = discord_request("POST", f"https://discord.com/api/v10/guilds/{parent['guild_id']}/channels", token, json=payload).json()
    return channel["id"]


def create_hand_thread(channel_id: str, token: str, hand: Hand, zone: str) -> str:
    root = discord_request("POST", f"https://discord.com/api/v10/channels/{channel_id}/messages", token, json={"content": f"{hand.label}"}).json()
    name = re.sub(r"[^a-z0-9-]", "-", hand.label.lower()).strip("-")[:90] or "hand"
    thread = discord_request("POST", f"https://discord.com/api/v10/channels/{channel_id}/messages/{root['id']}/threads", token, json={"name": name}).json()
    return thread["id"]


def send_hand(thread_id: str, token: str, hand: Hand, zone: str) -> None:
    when = datetime.fromtimestamp(hand.timestamp, ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M")
    filename = Path(hand.screenshot).name
    payload = {"embeds": [{
        "title": f"{hand.label}｜{when}",
        "url": hand.url,
        "description": "開啟 BBO LIN 牌局",
        "image": {"url": f"attachment://{filename}"},
    }]}
    with open(hand.screenshot, "rb") as image:
        discord_request("POST", f"https://discord.com/api/v10/channels/{thread_id}/messages", token, files={"files[0]": (filename, image, "image/png")}, data={"payload_json": json.dumps(payload, ensure_ascii=False)})


def read_state(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {"last_stable_time": 0, "sent_urls": [], "threads": {}}


def date_range_timestamps(start_date: str, end_date: str, zone: str) -> tuple[int, int]:
    """Return an inclusive local-date range as [start, end) Unix timestamps."""
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        tz = ZoneInfo(zone)
    except (ValueError, KeyError) as exc:
        raise ValueError("日期必須是 YYYY-MM-DD，且時區必須有效") from exc
    start_ts = int(datetime.combine(start, midnight.min, tzinfo=tz).timestamp())
    end_ts = int(datetime.combine(end, midnight.min, tzinfo=tz).timestamp()) + 86400
    if end_ts <= start_ts:
        raise ValueError("結束日期必須不早於開始日期")
    return start_ts, end_ts


def query_url(target: str, start_ts: int, end_ts: int) -> str:
    return MYHANDS_RESULTS_URL + "?" + urlencode({"username": target, "start_time": start_ts, "end_time": end_ts})


def scrape_range(cfg, start_ts: int, end_ts: int, screenshot_dir: Path) -> list[Hand]:
    """Login to BBO, query one date range, and collect its screenshots."""
    from playwright.sync_api import sync_playwright

    username, password = os.getenv("BBO_USERNAME"), os.getenv("BBO_PASSWORD")
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
            page.goto(query_url(cfg.target_user, start_ts, end_ts), wait_until="domcontentloaded", timeout=60_000)
            return collect_hands(page, cfg.target_user, cfg.timezone, screenshot_dir)
        finally:
            context.close()
            browser.close()


def sync_range(cfg, start_ts: int, end_ts: int, now: int | None = None) -> tuple[int, int]:
    """Search, group, and publish a range. Returns (found, sent)."""
    now = now or int(time.time())
    state = read_state(cfg.state)
    screenshot_dir = Path("screenshots")
    screenshot_dir.mkdir(exist_ok=True)
    hands = [h for h in scrape_range(cfg, start_ts, end_ts, screenshot_dir) if start_ts <= h.timestamp < end_ts]
    stable, pending = stable_groups(hands, now, cfg.group_minutes * 60)
    stable = [organize_batch(ordered_batch(batch), screenshot_dir, cfg.timezone) for batch in stable]
    if pending:
        pending = organize_batch(ordered_batch(pending), screenshot_dir, cfg.timezone)
    sent = set(state.get("sent_urls", []))
    group_channels = state.setdefault("group_channels", {})
    hand_threads = state.setdefault("hand_threads", state.get("threads", {}))
    sent_count = 0
    for batch in stable:
        new = [h for h in batch if h.url not in sent]
        if not new:
            continue
        key = str(min(hand.timestamp for hand in batch))
        if not cfg.dry_run:
            group_time = datetime.fromtimestamp(int(key), ZoneInfo(cfg.timezone)).strftime("%Y-%m-%d-%H%M")
            group_channel = group_channels.get(key) or create_group_channel(cfg.channel_id, cfg.token, group_time)
            group_channels[key] = group_channel
            for hand in new:
                thread_id = hand_threads.get(hand.url) or create_hand_thread(group_channel, cfg.token, hand, cfg.timezone)
                hand_threads[hand.url] = thread_id
                send_hand(thread_id, cfg.token, hand, cfg.timezone)
                sent.add(hand.url)
                sent_count += 1
        else:
            sent.update(h.url for h in new)
            sent_count += len(new)
    if not cfg.dry_run:
        state["sent_urls"] = list(sent)[-5000:]
        if stable and not pending:
            state["last_stable_time"] = max(h.timestamp for batch in stable for h in batch)
        cfg.state.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return len(hands), sent_count


def main() -> int:
    cfg = parse_args()
    now = int(time.time())
    state = read_state(cfg.state)
    start = state.get("last_stable_time") or now - cfg.days * 86400
    print(f"查詢起點 Unix timestamp: {start}；回溯 {cfg.days} 天（若 state 為空）")
    username, password = os.getenv("BBO_USERNAME"), os.getenv("BBO_PASSWORD")
    token, channel_id = os.getenv("DISCORD_BOT_TOKEN"), os.getenv("DISCORD_CHANNEL_ID")
    if not cfg.manual_login and (not username or not password):
        raise SystemExit("缺少 BBO_USERNAME 或 BBO_PASSWORD")
    if not cfg.dry_run and (not token or not channel_id):
        raise SystemExit("建立討論串需要 DISCORD_BOT_TOKEN 與 DISCORD_CHANNEL_ID")
    cfg.token, cfg.channel_id = token, channel_id
    end = now + 86400
    found, sent_count = sync_range(cfg, start, end, now)
    print(f"查到 {found} 筆；本次送出 {sent_count} 筆")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
