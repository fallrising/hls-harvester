# hls-harvester

> **狀態：M1 完成。** 可直接以清單 URL 下載（`get --playlist`）：分段並行下載、暫時性錯誤重試、續傳、不連續區段與 DRM 偵測，輸出經 ffprobe 驗證。頁面擷取（M2）與播放代理（M3）尚未實作。里程碑與「可正式使用」的條件見 [docs/SDD.md §8](docs/SDD.md#8-里程碑)，實作取捨見 [docs/decisions.md](docs/decisions.md)。

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

## 目前用法（M1）

```sh
# 直接給 HLS 清單 URL（master 或 media playlist），不經瀏覽器擷取
hls-harvester get --playlist "https://example.com/path/master.m3u8" --quality 720p --output clip.mp4
hls-harvester get --playlist "<同一 URL>" --output clip.mp4 --resume   # 中斷後續傳
```

結束碼見 [SDD §3.8](docs/SDD.md#38-設定日誌與結束碼)。

## Development

需要 Python 3.11 以上與 FFmpeg/ffprobe 6.0 以上（測試會用 FFmpeg 即時產生合成 HLS 樣本）。

```sh
python3 -m venv .venv && . .venv/bin/activate
make install            # pip install -e ".[dev]"
playwright install chromium   # 選用；M2 起的頁面擷取才需要（下載不需瀏覽器）
hls-harvester doctor    # 檢查環境
```

```sh
make lint        # ruff check + ruff format --check
make typecheck   # mypy --strict src
make test        # pytest
make check       # 以上全部
```

測試 fixture（`tests/fixtures/`）：`media.py` 產生合成 HLS 樣本，`server.py` 是兩個 host（origin／CDN）的 aiohttp 伺服器，可依路由注入延遲、429、503、401／403、重新導向與斷線，並記錄每個請求的標頭。pytest fixture `hls_samples`、`fixture_server` 定義在 `tests/conftest.py`。
