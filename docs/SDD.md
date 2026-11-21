# Software Design Document: hls-harvester

## 1. 概述

**hls-harvester** 是一個可程式化、標準化的個人串流處理系統。目標是把「手動打開瀏覽器、按 F12 抓 m3u8」變成「丟入頁面 URL → 後台拿到乾淨串流並本地播放或下載」。

系統專為具備以下共同特徵的影片站點設計：
- 影片使用 HLS / m3u8（動態 token，每次請求地址都會變更）
- 頁面本身充滿廣告腳本與反廣告欄截機制
- 真正的串流只會在播放器載入後才出現在網路請求中

**設計原則**：後台擷取 + 本地乾淨播放/下載，完全避開在瀏覽器中直接觀看原站。廣告腳本與跳轉被隔離在後台瀏覽器實例中，主瀏覽器不受影響。

系統提供**通用處理流程**，透過插件式適配器處理不同站點差異，而非寫死特定網址。

## 2. 系統架構

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│  輸入層           │     │  擷取層            │     │  快取層               │
│  (CLI / API)    │────▶│  Playwright       │────▶│  Redis / SQLite      │
│  丟入頁面 URL    │     │  欄截 m3u8        │     │  URL + headers + TTL │
│                 │     │  提取標題          │     │                     │
└─────────────────┘     └──────────────────┘     └─────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│  播放 / 下載層                                                     │
│  • 本地 HLS Proxy（重寫 playlist + 帶正確 headers 抓 segments）     │
│  • 或 ffmpeg 直接下載完整影片（MP4 / TS）                          │
│  • 最後用 VLC / IINA / hls.js / 本地播放器播放，完全不經過原站     │
└────────────────────────────────────────────────────────────────────────┘
```

## 3. 核心模組

### 3.1 輸入層
- CLI：`hls-harvester <page-url> [--download] [--quality] [--output]`
- 可選 API / HTTP endpoint（未來擴充）
- 輸入：任意影片頁面 URL
- 輸出：處理中狀態、最終本地檔案路徑或代理播放 URL

### 3.2 擷取層（最重要）
- 使用 **Playwright**（Chromium，可加 stealth 插件如 playwright-stealth）
- 流程：
  1. 載入頁面 URL
  2. 自動點擊播放按鈕（若存在）
  3. 監聽 `response` 事件，過濾：
     - URL 包含 `.m3u8`
     - 或 `content-type` 包含 `mpegurl` / `video`
  4. 記錄必要請求標頭：`Referer`、`Cookie`、`User-Agent`、其他自訂標頭（防盜鏈）
  5. 從頁面提取影片標題（`<title>`、`og:title`、`h1`、或站點特定 selector），用於檔名
  6. 可選：等待 network idle 或特定超時
- 完全在後台 headless 執行，不影響用戶主瀏覽器
- 失敗時可快速重試（可設定次數），或進入手動 fallback 模式

**通用欄截器**：預設使用網路請求欄截。針對特定站點可註冊適配器（Adapter）：
- 通用 Adapter：純欄截
- 站點專用 Adapter：例如先 POST 到內部 API 再解碼、或特定 DOM 操作後再欄截

### 3.3 快取層
- Key：正規化後的頁面 URL 或影片 ID（可從 URL path 提取）
- 存儲：
  - `m3u8_url`
  - `headers`（dict）
  - `title`（提取到的影片名稱）
  - `qualities`（可選，多畫質）
  - `expires_at`
- TTL：5–15 分鐘（視站點 token 失效速度調整）
- 相同影片短時間內重複請求直接回傳快取，不再啟動瀏覽器

### 3.4 播放 / 下載層
兩條主要路徑：

**A. 完整下載模式（預設）**
- 使用 ffmpeg 直接下載：
  ```bash
  ffmpeg -headers "Referer: ...\r\nCookie: ...\r\n" \
         -i "m3u8_url" \
         -c copy \
         -movflags +faststart \
         "{sanitized_title}.mp4"
  ```
- 支援選擇畫質（若 master playlist 有多個 variant）
- 產出乾淨 MP4，無廣告、無緩衝中斷
- 檔名來自頁面提取的標題，自動清理非法字元

**B. 實時串流模式**
- 本地 HLS Proxy（Python aiohttp 或 Node http）：
  1. 抓取原 m3u8
  2. 重寫 playlist，把 segment URL 指向本地代理
  3. 代理請求時帶入原始 headers
- 用戶打開 `http://localhost:<port>/playlist.m3u8` 即可用 VLC / IINA / hls.js 播放
- 可選同時錄制到檔案

### 3.5 適配器層（通用性關鍵）
- 基礎：`BaseAdapter` 實現通用欄截 + 標題提取
- 站點專用：繼承並覆寫 `extract_m3u8()`、`extract_title()`
- 註冊機制：根據 domain 自動選擇適配器，未知站點使用通用適配器
- 失敗降級：專用失敗時自動回退到通用欄截

## 4. 使用流程

1. 用戶在終端機輸入頁面 URL
2. 系統檢查快取；命中則直接使用緩存的 m3u8
3. 未命中：後台啟動 Playwright 擷取 m3u8 + headers + title（約 5–15 秒）
4. 寫入快取
5. 根據選項：
   - 下載完整影片（ffmpeg）
   - 或啟動本地 HLS Proxy
6. 用戶用本地播放器觀看，或直接取得 MP4 檔案

全程無廣告、無跳轉、無原站 buffering 中斷。

## 5. 技術棧建議

- 主語言：Python（Playwright + aiohttp/redis/sqlite + subprocess ffmpeg）
- 備選：Node.js + Puppeteer
- 快取：Redis（生產）或 SQLite（單機簡單）
- 下載/轉碼：FFmpeg（系統已安裝）
- 反偵測：playwright-stealth 或自定義 user-agent / viewport
- Cloudflare 挑戰：視站點狀況，可選加入代理或手動解決

## 6. 檔案結構建議

```
hls-harvester/
├── README.md
├── docs/
│   └── SDD.md
├── src/
│   ├── cli.py
│   ├── capturer.py          # Playwright 欄截
│   ├── cache.py
│   ├── downloader.py        # ffmpeg 封裝
│   ├── proxy.py             # HLS Proxy
│   └── adapters/
│       ├── base.py
│       └── generic.py
├── requirements.txt
└── .gitignore
```

## 7. 風險與注意事項

- 站點結構頻繁更動：依賴適配器模式並支援快速重試
- Token 失效：快取 TTL 短，失效時自動重新擷取
- 法律與使用範圍：僅供個人學習與合法使用，請遵守站點條款與著作權法規
- 廣告完全避開：因為不在主瀏覽器執行原站腳本

## 8. 未來擴充

- 多畫質自動選擇
- 並行下載隊列
- Web UI
- 更多站點適配器
- Docker 化部署

---

**核心原則**：Playwright 後台欄截 + 本地乾淨下載/播放。通用流程優先，站點差異透過適配器處理。
