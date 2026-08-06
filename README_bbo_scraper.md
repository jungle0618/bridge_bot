# BBO 牌局離線保存器

這個腳本會開啟可見的 Chromium，讓使用者在網頁中手動登入 BBO，再依日期範圍查詢指定玩家，保存每副牌的：

- 原始牌局網址（`hands.csv`、`hands.json`）
- 牌局頁面的 HTML（`pages/`）
- 牌局頁面截圖（`screenshots/`）
- 可用瀏覽器直接打開的離線索引（`index.html`）

## 安裝

```bash
python -m pip install -r requirements-bbo.txt
python -m playwright install chromium
```

## 執行

執行後若 BBO 將頁面導向登入畫面，請直接在瀏覽器頁面輸入帳號與密碼；程式會自動偵測登入完成並繼續，不需要回到終端機按 Enter。

```bash
python bbo_hand_scraper.py \
  --target-user wei1011 \
  --start-date 2026-07-07 \
  --end-date 2026-08-07 \
  --output bbo_hands_archive
```

日期格式是 `YYYY-MM-DD`，包含開始日、不包含結束日；預設時區是 `Asia/Taipei`。例如查詢 2026-08-06 單日：

```bash
python bbo_hand_scraper.py --start-date 2026-08-06 --end-date 2026-08-07
```

完成後開啟 `bbo_hands_archive/index.html`。HTML 是查詢當下保存的頁面內容；外部 CSS/圖片若要在完全斷網時也顯示，需另外下載網站資源，截圖則不受影響。
