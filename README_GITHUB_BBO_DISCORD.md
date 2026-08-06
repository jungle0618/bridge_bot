# BBO → Discord GitHub Repository

這個專案每天執行一次，使用 BBO My Hands 原本的日期表單查詢 `wei1011` 的牌局，並把已經穩定的牌局網址送到 Discord。第一次執行會查最近 20 天。

## GitHub Secrets

在 Repository → Settings → Secrets and variables → Actions 新增：

- `DISCORD_BOT_TOKEN`：Discord Bot Token
- `DISCORD_CHANNEL_ID`：要建立討論串的文字頻道 ID

BBO 帳號與密碼目前依專案設定直接寫在 `.github/workflows/bbo-discord-sync.yml`。

Bot 必須加入 Discord Server，並在參考頻道具有：

- `View Channel`
- `Manage Channels`（建立每組牌的新頻道）
- `Send Messages`
- `Create Public Threads`
- `Send Messages in Threads`

若要重新掃描最近 20 天，刪除 Repository 根目錄的 `state.json` 後重新執行一次 Workflow。正常排程不應刪除它，因為它用來避免重複發送。

## 時間分組規則

程式預設以 30 分鐘分組。每一組會建立一個新 Discord 頻道；每一副牌會在該頻道建立一個討論串，並按照牌號由小到大發送。若最新一組牌局的最後一牌距離執行時間少於 30 分鐘，整組會標記為 pending，不會送 Discord，也不會更新成已完成。

## 手動執行

安裝依賴後：

```bash
python -m pip install -r requirements-bbo-discord.txt
python -m playwright install chromium
python bbo_discord_sync.py --dry-run
```

本機沒有設定 BBO 帳密時，可以使用可見瀏覽器手動登入：

```bash
python bbo_discord_sync.py --manual-login --dry-run
```

## Discord 內手動搜尋

若要在 Discord 內輸入指令，需另外長時間執行常駐 Bot（不適合放在每天執行一次的 GitHub Actions）：

```bash
python bbo_discord_bot.py
```

常駐 Bot 使用同一組 `BBO_USERNAME`、`BBO_PASSWORD`、`DISCORD_BOT_TOKEN`，並可選設定 `BBO_TARGET_USER`、`BBO_TIMEZONE`、`BBO_STATE`。

Slash Command 用法：

- `/bbo_search start:2026-07-01 end:2026-07-20`：搜尋 7 月 1 日至 7 月 20 日（首尾皆包含）。
- `/bbo_search`：從 `state.json` 的 `last_stable_time` 搜尋到現在；沒有紀錄時回溯 20 天。

日期區間搜尋仍會沿用原本的分組、截圖、Discord 頻道／Thread 建立和 URL 去重邏輯。Bot 必須邀請 `applications.commands` scope，且執行帳號有使用該 Slash Command 的權限。

GitHub Actions 使用 `workflow_dispatch` 可手動觸發，也會每天 00:00 UTC 自動執行。`state.json` 會由 Actions commit 回 Repository，保存上次已處理時間與已送出的網址。
