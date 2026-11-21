# hls-harvester

> **狀態：M2 完成。** 可從頁面 URL 擷取並下載：獨立瀏覽器擷取（彈窗、跳轉、iframe、預載、多片選擇）、`login` 工作階段、憑證只送往適用的 host、過期時重新擷取。也可用 `--playlist` 直接下載清單。播放代理（M3）尚未實作。里程碑見 [docs/SDD.md §8](docs/SDD.md#8-里程碑)，實作取捨見 [docs/decisions.md](docs/decisions.md)。

通用化 HLS / m3u8 串流擷取與本地乾淨播放／下載工具，供你有權存取與保存的影音使用。

把「手動打開瀏覽器、F12 找 m3u8、處理彈窗與跳轉」變成「丟入頁面 URL → 獨立瀏覽器擷取串流 → 本地播放或下載完整影片」。

適用於具備以下特徵的頁面：
- 影片為 HLS / m3u8，清單網址帶有短效簽章，每次都會變
- 頁面有彈窗、跳轉等腳本，干擾手動操作
- 真正的串流只在播放器載入後才出現在網路請求中

**不支援**：DRM 保護的影音（Widevine、FairPlay、PlayReady、SAMPLE-AES）、繞過付費牆／登入／地區限制等存取控制、繞過 CAPTCHA 或機器人偵測。使用者須自行確認對內容的權利及網站條款。

詳細系統設計請見 [docs/SDD.md](docs/SDD.md)。

## 目標
- 獨立的 Playwright 瀏覽器擷取（不干擾你的主瀏覽器），可載入你自行登入的工作階段
- 自動處理彈窗、新分頁與跳轉，辨識正確的播放清單
- 提取頁面影片標題作為檔名
- 分段並行下載，支援憑證過期重取與中斷續傳，輸出經 ffprobe 驗證
- 本地 HLS 代理供 VLC、IINA 或 hls.js 乾淨播放
- 通用處理流程，以站點適配器處理差異

## 目前用法（M2）

```sh
playwright install chromium            # 擷取需要 Chromium

# 從影片頁面擷取並下載（預設檔名取自頁面標題）
hls-harvester get "https://example.com/watch/123" --quality 720p

# 需要登入的網站：先在獨立瀏覽器中自行登入，保存工作階段（檔案權限 0600）
hls-harvester login "https://example.com" --storage-state ~/.config/hls-harvester/example.json
hls-harvester get "https://example.com/watch/123" --storage-state ~/.config/hls-harvester/example.json

# 已知清單 URL 時可略過擷取
hls-harvester get --playlist "https://cdn.example.com/path/master.m3u8"
```

中斷後加 `--resume` 續傳。結束碼見 [SDD §3.8](docs/SDD.md#38-設定日誌與結束碼)。

## Development

需要 Python 3.11 以上與 FFmpeg/ffprobe 6.0 以上（測試會用 FFmpeg 即時產生合成 HLS 樣本）。

```sh
python3 -m venv .venv && . .venv/bin/activate
make install            # pip install -e ".[dev]"
playwright install chromium   # 頁面擷取與其測試需要
hls-harvester doctor    # 檢查環境
```

```sh
make lint        # ruff check + ruff format --check
make typecheck   # mypy --strict src
make test        # pytest
make check       # 以上全部
```

無法執行 `playwright install chromium` 的環境，可用 `HLSH_TEST_CHROMIUM=/path/to/chrome make test` 指定 Chromium（設定檔對應選項為 `browser_executable`）。

測試 fixture（`tests/fixtures/`）：`media.py` 產生合成 HLS 樣本，`server.py` 是兩個 host（origin／CDN）的 aiohttp 伺服器，可依路由注入延遲、429、503、401／403、重新導向與斷線，並記錄每個請求的標頭。`site.py` 是模擬播放器的測試頁（預載、彈窗、iframe、多片、blob、登入、憑證轉送、600 分段長片）。pytest fixture `hls_samples`、`fixture_server`、`site`、`browser` 定義在 `tests/conftest.py`。
