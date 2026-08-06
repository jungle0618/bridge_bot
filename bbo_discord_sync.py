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
    first = datetime.fromtimestamp(batch[0].timestamp, ZoneInfo(zone))
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
    print(f"查詢起點 Unix timestamp: {start}；回溯 {cfg.days} 天（若 state 為空）")
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
    stable = [organize_batch(batch, screenshot_dir, cfg.timezone) for batch in stable]
    if pending:
        pending = organize_batch(pending, screenshot_dir, cfg.timezone)
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
