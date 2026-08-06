#!/usr/bin/env python3
"""Download BBO My Hands results and save an offline archive.

Login is manual: Chromium is opened visibly and the user types credentials
into the real BBO web page.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import date, datetime, time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


MYHANDS_URL = "https://www.bridgebase.com/myhands/hands.php"
LOGIN_URL = "https://www.bridgebase.com/myhands/index.php?&from_login=1"
DEFAULT_TARGET = "wei1011"
DEFAULT_START_DATE = "2026-07-07"
DEFAULT_END_DATE = "2026-08-07"
DEFAULT_TIMEZONE = "Asia/Taipei"


def args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Archive BBO My Hands records offline")
    p.add_argument("--target-user", default=DEFAULT_TARGET, help="User whose hands are queried")
    p.add_argument("--start-date", default=DEFAULT_START_DATE, help="開始日期 YYYY-MM-DD（包含當日）")
    p.add_argument("--end-date", default=DEFAULT_END_DATE, help="結束日期 YYYY-MM-DD（不包含當日）")
    p.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="日期時區，預設 Asia/Taipei")
    p.add_argument("--output", type=Path, default=Path("bbo_hands_archive"))
    p.add_argument("--delay", type=float, default=0.7, help="Seconds between hand pages")
    p.add_argument("--limit", type=int, help="Only archive the first N hands")
    return p.parse_args()


def date_timestamp(value: str, timezone: str) -> int:
    try:
        parsed = date.fromisoformat(value)
        zone = ZoneInfo(timezone)
    except (ValueError, KeyError) as exc:
        raise SystemExit(f"日期或時區格式錯誤：{value!r} / {timezone!r}") from exc
    return int(datetime.combine(parsed, time.min, tzinfo=zone).timestamp())


def make_query(target: str, start_date: str, end_date: str, timezone: str) -> str:
    start = date_timestamp(start_date, timezone)
    end = date_timestamp(end_date, timezone)
    if end <= start:
        raise SystemExit("--end-date 必須晚於 --start-date")
    return MYHANDS_URL + "?" + urlencode({"username": target, "start_time": start, "end_time": end})


def open_query_and_wait_for_login(page, target_url: str) -> None:
    """Open the query and wait until a manual BBO login has completed."""
    page.goto(target_url, wait_until="domcontentloaded")
    if "/myhands/index.php" not in page.url:
        return

    print("BBO 要求登入，請在已開啟的瀏覽器中手動輸入帳號與密碼。")
    print("完成登入後程式會自動繼續，無需在終端機按 Enter。")
    # Avoid depending on a BBO-specific form selector: the site has changed
    # its login markup several times. A successful session shows 'Logged in as'
    # on the My Hands page or redirects away from index.php.
    for _ in range(600):
        page.wait_for_timeout(1000)
        if "/myhands/index.php" not in page.url:
            return
        if "Logged in as" in page.content():
            return
    raise RuntimeError("等待登入超過 10 分鐘；請確認帳密、驗證碼與 BBO 網路連線。")


def hand_links(page, target_url: str) -> list[str]:
    page.goto(target_url, wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    links: list[str] = []
    for anchor in page.locator("a").all():
        href = anchor.get_attribute("href") or ""
        if "handviewer.html" in href or "lin=" in href:
            absolute = canonical_hand_url(urljoin(page.url, href))
            if absolute and absolute not in links:
                links.append(absolute)
    # A few BBO pages expose the viewer URL in script/text rather than anchors.
    html = page.content()
    for match in re.findall(r"(?:https?://|/)[^\"'<>\\ ]*handviewer\.html\?[^\"'<>\\ ]+", html):
        clean = canonical_hand_url(urljoin(page.url, match.replace("&amp;", "&")))
        if clean and clean not in links:
            links.append(clean)
    return links


def canonical_hand_url(url: str) -> str | None:
    """Return the same bbo=y&lin= URL shape used by BBO handviewer."""
    parsed = urlparse(url)
    if "lin=" not in parsed.query:
        return None
    params = parse_qs(parsed.query, keep_blank_values=True)
    if not params.get("lin", [None])[0]:
        return None
    # Preserve BBO's existing percent encoding; urlencode would double-escape it.
    query = parsed.query
    if "bbo=" not in query:
        query = "bbo=y&" + query
    return "https://www.bridgebase.com/tools/handviewer.html?" + query


def safe_name(index: int, url: str) -> str:
    query = parse_qs(urlparse(url).query)
    lin = query.get("lin", [""])[0]
    board = re.search(r"(?:ah\||Board%20|Board )([0-9]+)", lin, re.I)
    label = f"board-{board.group(1)}" if board else f"hand-{index:04d}"
    digest = hashlib.sha1(url.encode()).hexdigest()[:10]
    return f"{index:04d}-{label}-{digest}"


def write_index(out: Path, records: list[dict]) -> None:
    (out / "hands.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out / "hands.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=["index", "source_url", "offline_html", "screenshot"])
        writer.writeheader()
        writer.writerows(records)
    rows = "\n".join(
        f'<li><a href="{r["offline_html"]}">第 {r["index"]} 副牌（離線 HTML）</a> '
        f'<a href="{r["screenshot"]}">[截圖]</a> '
        f'<a href="{r["source_url"]}">[原始網址]</a></li>'
        for r in records
    )
    html = f"""<!doctype html><meta charset="utf-8"><title>BBO hands archive</title>
<h1>BBO 牌局離線索引</h1><p>共 {len(records)} 副牌。HTML 與截圖已保存於本資料夾。</p>
<ol>{rows}</ol>"""
    (out / "index.html").write_text(html, encoding="utf-8")


def main() -> int:
    cfg = args()
    out = cfg.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "screenshots").mkdir(exist_ok=True)
    (out / "pages").mkdir(exist_ok=True)
    target_url = make_query(cfg.target_user, cfg.start_date, cfg.end_date, cfg.timezone)
    records: list[dict] = []

    with sync_playwright() as pw:
        # Manual login requires a visible browser window.
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-TW")
        page = context.new_page()
        try:
            open_query_and_wait_for_login(page, target_url)
            urls = hand_links(page, target_url)
            if cfg.limit:
                urls = urls[: cfg.limit]
            print(f"找到 {len(urls)} 副牌，開始保存…")
            for index, url in enumerate(urls, 1):
                name = safe_name(index, url)
                hand_page = context.new_page()
                try:
                    hand_page.goto(url, wait_until="networkidle", timeout=45_000)
                    hand_page.screenshot(path=str(out / "screenshots" / f"{name}.png"), full_page=True)
                    (out / "pages" / f"{name}.html").write_text(hand_page.content(), encoding="utf-8")
                    records.append({"index": index, "source_url": url, "offline_html": f"pages/{name}.html", "screenshot": f"screenshots/{name}.png"})
                    print(f"[{index}/{len(urls)}] {name}")
                except PlaywrightTimeoutError:
                    print(f"跳過（載入逾時）：{url}", file=sys.stderr)
                finally:
                    hand_page.close()
                    page.wait_for_timeout(int(cfg.delay * 1000))
            write_index(out, records)
        finally:
            context.close()
            browser.close()
    print(f"完成：{out / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
