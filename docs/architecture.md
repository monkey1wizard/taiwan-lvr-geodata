# 架構與專案邊界

本文件描述目前程式的模組責任、相依方向、資料來源與專案邊界。使用命令請從根目錄 [README](../README.md) 開始。處理階段見[資料處理流程](資料處理流程.md)，內部資料關聯見[資料契約](資料契約.md)，公開格式見[輸出欄位](輸出欄位.md)。

## 專案責任

`taiwan-lvr-geodata` 負責讀取實價登錄來源、保存來源觀測、正規化交易及地址、以固定地址資料離線定位、管理 TGOS 人工交換、回補定位結果，以及產生可追溯的地理資料。

`taiwan-address-data` 是地址基底與後續地址補充的獨立專案。此專案只讀取其固定版本作為輸入，並可產生地址補充候選。跨專案的實際匯入與更新另行記錄和驗收。舊 `taiwan-lvr-geojson` 僅供唯讀參考。

外部來源包括內政部實價登錄 ZIP、固定版本地址資料、臺北市官方來源描述、路名參考、補字規則及人工 TGOS 回傳。各來源需按新 run 重新核對，不以檔案存在、舊清冊或舊報告推定完整、正確或有使用權限。

2026-10-12 重建要求禁止新流程讀取既有衍生處理結果，包括 normalized／converted、地址池／index／state、aliases、TGOS 採用結果、review 裁定、衍生 cache、快照、維護包、公開輸出及舊統計。原始來源檔仍需重新核驗。舊 TGOS 回傳結果已於 2026-10-13 由擁有者決定廢棄，不重核、不納入任何新結果；本機已無這些檔案。隔離護欄已於 2026-10-12 實作（R07-6a）：沒有 `rebuild_epoch = 2026-10-12` 的快照與 run 一律拒絕讀入，規則版本不符的快照也拒絕；`run-full` 只接受帶本次紀元的來源清冊與地址來源描述；2026-10-13 決定取消人工簽核，R07-6c 已移除 `source_check` 檢查，清冊改記 `scanned_at`。舊 TGOS 帳本與舊回傳在本機已不存在，不轉接、不擋重送；新重建以新帳本從零開始，離線定位不到的門牌全部重新送查。 `--ledger` 轉接待 R09-2 移除。

## 模組責任

目前 `lvr_pipeline/` 有 45 個正式 Python 模組。下表按責任列出模組群組及入口。實際函式及命令以程式碼與各命令 `--help` 為準。

| 模組 | 現行責任 |
| --- | --- |
| `cli.py`、`__main__.py` | 命令列解析、命令分派與模組入口 |
| `pipeline.py` | `run-full` 階段編排、來源核對、階段續跑與 run report |
| `transactions/` | 交易來源清單、ZIP 主檔讀取、欄位正規化與日期驗證 |
| `contracts/` | Arrow／Parquet 資料契約與列驗證 |
| `storage/` | 執行目錄、快照和 Parquet 儲存 |
| `addresses/`、`address.py` | 地址補字、文字解析、門牌鍵及目前保留的程式介面 |
| `addresses/pool.py`、`offline/` | 地址成員池、離線索引、候選比對與定位解析 |
| `results/` | 例外、待查核、交叉比對與座標 reconcile 結果 |
| `tgos/` | 批次狀態、交換檔及回傳驗證；TGOS 目前由個別命令執行 |
| `output/` | 新版 22 欄逐門牌輸出：`rows.py` 建立資料列，`parquet.py`、`csv.py` 寫出 GeoParquet 與每月 CSV，GeoPackage 與 GeoJSON 已於 R07-12f 移除，`build.py` 串接。舊契約模組已於 R07-8b 刪除 |
| `results/address_patch.py` | 地址補充候選入口；新版完整契約尚待實作。`results/backfill.py` 已於 R07-8b 刪除，回補由 R10-1 重新實作 |
| `converted.py`、`source_ref.py`、`tx_date.py` | 轉換快照、來源參照及相容日期入口 |
| `0_parse_raw.py`、`1_normalize.py` | 現行程式保留的舊入口 |

本專案不含前端、線上資料庫服務、背景排程服務，也不負責發布或修改地址資料專案。GitHub Actions 執行 Linux 合成資料驗證及已配置的公開驗證工作流程。

## 相依方向

高層模組依賴底層模組，底層契約不依賴 CLI 或流程編排。交易讀取和契約模組提供結構化來源資料。地址處理模組被地址池及定位結果使用。`pipeline.py` 編排來源核對、交易轉換、地址處理、review 與新版輸出階段。`cli.py` 負責命令分派，不承載資料規則。

下表箭頭一律表示左側模組匯入或使用右側模組，不表示實際執行順序。`run-full` 不編排 TGOS 命令。

| 上層模組 | 依賴的下層模組 | 邊界 |
| --- | --- | --- |
| `cli.py` | `pipeline.py` 及各獨立命令的實作模組 | 分派命令；TGOS 是獨立命令 |
| `pipeline.py` | `transactions/`、`contracts/`、`storage/`、地址／離線／review 模組及 `output/build.py` | `run-full` 不呼叫 TGOS 狀態流程 |
| `addresses/pool.py`、`offline/` | `addresses/`、`contracts/`、`storage/` | 地址鍵及定位使用共用地址規則 |
| `tgos/batches.py` | `tgos/exchange.py` 及狀態儲存能力 | 建立批次與交換檔 |
| `tgos/validate.py` | `tgos/batches.py`、`tgos/exchange.py` | 驗證回傳並更新 TGOS 結果 |
| `output/build.py` | `output/rows.py`、`parquet.py`、`csv.py` | 一次走訪資料列寫出兩種格式 |
| `output/rows.py` | `storage/`、`results/reconcile.py` | 讀取 converted 與定位狀態快照 |

新版目標模組拆分與刪除項目見[重建企劃](plans/重建企劃.md)。規劃中的目錄尚未全部落地，表中現行模組不代表目標模組已建立。

本表記錄程式現況，不保證舊入口、舊格式或舊結果可供新流程相容使用。新 run 受 R07-6a 隔離護欄約束：所有經 `SnapshotStore` 讀取的階段快照都須帶本次 `rebuild_epoch`。舊公開輸出目錄與維護包已不存在，其舊路徑已於 R07-8 刪除。`validated_roc_to_tx_yyyymm` 是明確保留的日期函式，不表示其他舊資料介面或結果可沿用。

## 目前能力與邊界

- `run-full` 在 `pipeline.py` 編排來源核對、離線階段、輸出階段及 run report，並支援階段續跑；這是現行實作事實，不授權續跑重建前快照。它會在寫入任何檔案前拒絕沒有本次紀元的來源清冊。
- TGOS 使用獨立 CLI 命令，必填 `--run-id`，狀態寫入 `data/runs/<run-id>/`（R09-2）；尚未由 `run-full` 管理 TGOS 狀態或回補，由 R09-8 實作。
- `transactions/read.py` 讀入交易主檔與 `_build.csv`、`_land.csv`、`_park.csv` 明細；`transactions/normalize.py` 依明細關聯契約把明細連回主檔，計數寫入 `detail_link`。
- `tgos/validate.py` 的 `import_tgos` 已檢查查詢與回傳集合一對一，並以相同批次和回傳雜湊支援冪等重匯。`addresses/identity.py` 的 `same_door` 允許回傳補上送出地址未寫的村里或鄰。R09-2 尚須檢查候選是否跨多個村里或鄰，未完成完整同址驗收。
- `output/` 產生新 22 欄門牌輸出與 CSV 23 欄，`run-full` 的輸出階段已改用它。`output/catalog.py` 寫 `catalog.json`，`output/verify.py` 執行格式間驗證，兩者都在輸出階段提交前執行。發布尚未實作（R08）。權威規格在[輸出欄位](輸出欄位.md)。
- 不設 `notice.json`（2026-10-13 決定）；本機 `data/output/` 目前不存在。`.gitignore` 已不忽略 `data/output/`，TGOS 命令與 `build-review` 以外的部分 CLI 預設仍使用 `data/work/`。這些是尚待程式或設定工作處理的差距。
- R07-4 的 58 批舊契約 run report 曾標示完成，但其 `output-release` 已移除。該歷史結果及相關衍生資料不作目前接受的候選、定位依據或新版驗收基準。

## 相依資料與版本

來源清單、來源描述與小型規則放在 `config/`。原始 ZIP、TGOS 交換檔、執行快照、工作區及產物位置依命令與[資料處理流程](資料處理流程.md)管理。所有規則版本、輸入雜湊與程式版本都應能回連到單次 run 及其快照。

目前本機 run/stage 路徑仍有歷史差異。新版公開目錄與檔案契約只以[輸出欄位](輸出欄位.md)為準，其他目標布局依企劃工作實作後才視為現行可用。`data/output/` 是 Git 公開位置。`.gitignore` 已於 R07-9 改為忽略 `data/*` 但不忽略 `data/output/`，所以 `data/output/` 下的檔案會被 Git 追蹤；只有經發布流程核准的公開輸出應放進該目錄。檢查輸出及失敗診斷存入 `data/tmp/<purpose>/`。維護包留在本機執行目錄，不公開。

## 相關文件

- [使用、安裝及現行命令](../README.md)
- [資料處理流程](資料處理流程.md)
- [地址規則](地址規則.md)
- [內部資料契約](資料契約.md)
- [公開輸出契約](輸出欄位.md)
- [剩餘工作與驗收](plans/重建企劃.md)
