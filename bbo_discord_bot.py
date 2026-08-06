#!/usr/bin/env python3
"""常駐 Discord Slash Command：從 BBO 查詢牌局並送回 Discord。"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace

import discord
from discord import app_commands

from bbo_discord_sync import date_range_timestamps, read_state, sync_range


class BBOBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        super().__init__(intents=intents)
        self.commands = app_commands.CommandTree(self)
        self.search_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        await self.commands.sync()


bot = BBOBot()


@bot.commands.command(name="bbo_search", description="搜尋 BBO 牌局並傳到 Discord")
@app_commands.describe(
    start="開始日期 YYYY-MM-DD（包含當日）；不填則從上次穩定紀錄開始",
    end="結束日期 YYYY-MM-DD（包含當日）；不填則查到現在",
)
async def bbo_search(interaction: discord.Interaction, start: str | None = None, end: str | None = None) -> None:
    if start is not None and end is None or start is None and end is not None:
        await interaction.response.send_message("開始日期和結束日期要一起填，或兩個都留空。", ephemeral=True)
        return
    if bot.search_lock.locked():
        await interaction.response.send_message("目前已有另一個搜尋正在執行，請稍後再試。", ephemeral=True)
        return

    state_path = Path(os.getenv("BBO_STATE", "state.json"))
    timezone = os.getenv("BBO_TIMEZONE", "Asia/Taipei")
    target = os.getenv("BBO_TARGET_USER", "wei1011")
    if not os.getenv("BBO_USERNAME") or not os.getenv("BBO_PASSWORD"):
        await interaction.response.send_message("Bot 主機缺少 BBO_USERNAME 或 BBO_PASSWORD。", ephemeral=True)
        return
    if not os.getenv("DISCORD_BOT_TOKEN") or not os.getenv("DISCORD_CHANNEL_ID"):
        await interaction.response.send_message("Bot 主機缺少 Discord 設定。", ephemeral=True)
        return

    now = int(time.time())
    try:
        if start and end:
            start_ts, end_ts = date_range_timestamps(start, end, timezone)
            range_label = f"{start}～{end}"
        else:
            state = read_state(state_path)
            start_ts = int(state.get("last_stable_time") or now - 20 * 86400)
            end_ts = now + 86400
            range_label = "上次穩定紀錄～現在"
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    cfg = SimpleNamespace(
        target_user=target,
        timezone=timezone,
        state=state_path,
        group_minutes=int(os.getenv("BBO_GROUP_MINUTES", "30")),
        manual_login=False,
        dry_run=False,
        token=os.environ["DISCORD_BOT_TOKEN"],
        channel_id=os.environ["DISCORD_CHANNEL_ID"],
    )
    try:
        async with bot.search_lock:
            found, sent = await asyncio.to_thread(sync_range, cfg, start_ts, end_ts, now)
        await interaction.followup.send(f"完成搜尋（{range_label}）：找到 {found} 筆，送出 {sent} 筆。")
    except Exception as exc:
        await interaction.followup.send(f"搜尋失敗：{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("缺少 DISCORD_BOT_TOKEN")
    bot.run(token)
