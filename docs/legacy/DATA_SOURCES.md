# 資料來源與下載

所有原始資料**不進 git**（檔案大、可重新下載），請手動下載到 `data/raw/`。

## 1. 內政部 LVR 實價登錄開放資料（核心輸入）

- 網址：https://plvr.land.moi.gov.tw/DownloadOpenData
- 選 **CSV 格式、全國 ZIP**，**不要解壓**
- 放到：`data/raw/<季>_lvr_landcsv.zip`（如 `115q1_lvr_landcsv.zip`）
- 授權：政府資料開放授權條款第 1 版（可重製/散布/商用，須顯名）
- 命名說明：政府用民國年，`115q1` = 民國 115 年第 1 季 = 西元 2026 Q1

## 2. taiwan-address-data 離線門牌庫（第 2 步離線定位用）

- 來源：zhengda / taiwan-address-data（GitHub），clone 到本機後設定 `ADDR_DB_DIR` 環境變數指向其根目錄
- 讀取路徑：`$ADDR_DB_DIR/roads/{縣市碼}-{路名}.csv`（預設 `C:\Code\taiwan-address-data`）
- 內容：`roads/` 內共 27,096 個路檔，含 FULL_ADDR / X / Y 欄（WGS84），涵蓋 17 縣市
- 更新：執行 `taiwan-address-data/scripts/update_addresses.py`（含台中市）
- 未涵蓋縣市：宜蘭、南投、連江、嘉義市（政府無開放門牌座標集），未命中者照常進 TGOS
- ⚠️ **第三方資料，授權與政府開放資料不同**，散布前請自行確認其授權條款；
  本專案不轉散布此 repo 的資料，僅在此記錄來源與設定方式。

## 3. TGOS 批次門牌地址比對服務（第 3 步殘餘定位用）

- 申請：成為 TGOS 會員 → 申請「**批次門牌地址比對服務（addrCompare）**」→ 取得金鑰
- 操作：登入 TGOS 批次網頁 → 上傳 `tgos_input/*.csv`（UTF-8-sig，≤10k/片）→ 下載比對結果 → 存入 `tgos_done/`
- 詳細步驟：[`docs/RESUBMIT_RUNBOOK.md`](./RESUBMIT_RUNBOOK.md)
- ⚠️ 現有金鑰**只授權批次服務**，非即時 QueryAddr API（見 `tgos-api-ip-locked` 記憶）

### TGOS 存取方式與紅線

| 方式 | 大量? | 何時用 |
|---|---|---|
| **① 批次 addrCompare（網頁上傳）** | ✅ | **主線**：全量殘餘地址批次送 |
| **② 圖台網頁 UI 逐筆查** | 🟡 僅小量 | 批次也找不到的殘餘（數十筆），人工保底 |
| **③ 即時 API（`TGComplexLocate`）** | ✅ 需另申請 | 有自備即時金鑰才適用 |

⚠️ **憑證紅線**：`map.tgos.tw` 圖台內嵌的公開金鑰**不可程式化重用**（視為盜用）。需大量即時定位請自行向 TGOS 申請即時金鑰。

## 策略

第 2 步離線門牌庫**無配額、免費**，先把能定位的全定掉；
剩下離線查不到的硬骨頭，走第 3 步 TGOS 批次（人工上傳，無額度限制）。
