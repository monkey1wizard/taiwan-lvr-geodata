# 架構與專案邊界

本文件描述目前程式的模組責任、相依方向、資料來源與專案邊界。使用命令請從根目錄 [README](../README.md) 開始。處理階段見[資料處理流程](資料處理流程.md)，內部資料關聯見[資料契約](資料契約.md)，公開格式見[輸出欄位](輸出欄位.md)。

## 專案責任

`taiwan-lvr-geodata` 負責讀取實價登錄來源、保存來源觀測、正規化交易及地址、以固定地址資料離線定位、管理 TGOS 人工交換、回補定位結果，以及產生可追溯的地理資料。

`taiwan-address-data` 是地址基底與後續地址補充的獨立專案。此專案只讀取其固定版本作為輸入，並可產生地址補充候選。跨專案的實際匯入與更新另行記錄和驗收。舊 `taiwan-lvr-geojson` 僅供唯讀參考。

外部來源包括內政部實價登錄 ZIP、固定版本地址資料、臺北市官方來源描述、路名參考、補字規則及人工 TGOS 回傳。各來源需按新 run 重新核對，不以檔案存在、舊清冊或舊報告推定完整、正確或有使用權限。

2026-10-12 重建要求禁止新流程讀取既有衍生處理結果，包括 normalized／converted、地址池／index／state、aliases、TGOS 採用結果、review 裁定、衍生 cache、快照、維護包、公開輸出及舊統計。原始來源檔與 TGOS 原始回傳仍需重新核驗，預設不繼承舊回傳定位。此隔離要求尚未由程式護欄實作；目前流程仍可能續用既有快照。

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
| `output/` | 現行舊契約的月檔、年度檔、組裝、驗證及發布函式。新版公開契約尚未實作 |
| `results/backfill.py`、`results/address_patch.py` | 現行歷史回補與地址補充候選入口；僅完成模組位置整理，新版完整契約尚待實作 |
| `converted.py`、`source_ref.py`、`tx_date.py` | 轉換快照、來源參照及相容日期入口 |
| `0_parse_raw.py`、`1_normalize.py` | 現行程式保留的舊入口 |

本專案不含前端、線上資料庫服務、背景排程服務，也不負責發布或修改地址資料專案。GitHub Actions 執行 Linux 合成資料驗證及已配置的公開驗證工作流程。

## 相依方向

高層模組依賴底層模組，底層契約不依賴 CLI 或流程編排。交易讀取和契約模組提供結構化來源資料。地址處理模組被地址池及定位結果使用。`pipeline.py` 編排來源核對、交易轉換、地址處理、review 與舊契約輸出階段。`cli.py` 負責命令分派，不承載資料規則。

下表箭頭一律表示左側模組匯入或使用右側模組，不表示實際執行順序。`run-full` 不編排 TGOS 命令。

| 上層模組 | 依賴的下層模組 | 邊界 |
| --- | --- | --- |
| `cli.py` | `pipeline.py` 及各獨立命令的實作模組 | 分派命令；TGOS 是獨立命令 |
| `pipeline.py` | `transactions/`、`contracts/`、`storage/`、地址／離線／review 模組及現有 `output/` | `run-full` 不呼叫 TGOS 狀態流程 |
| `addresses/pool.py`、`offline/` | `addresses/`、`contracts/`、`storage/` | 地址鍵及定位使用共用地址規則 |
| `tgos/batches.py` | `tgos/exchange.py` 及狀態儲存能力 | 建立批次與交換檔 |
| `tgos/validate.py` | `tgos/batches.py`、`tgos/exchange.py` | 驗證回傳並更新 TGOS 結果 |
| `output/yearly.py` | `output/monthly.py` | 使用月檔共用邏輯 |
| `output/verify.py` | 月檔、年度輸出及維護包讀取能力 | 檢查舊契約產物 |
| `output/publish.py`、`output/package.py` | `output/verify.py` 與較低層輸出模組 | 組裝及發布舊契約成品 |

新版目標模組拆分與刪除項目見[重建企劃](plans/重建企劃.md)。規劃中的目錄尚未全部落地，表中現行模組不代表目標模組已建立。

本表記錄程式現況，不保證舊入口、舊格式或舊結果可供新流程相容使用。新 run 必須受 R07-6a 隔離護欄約束；現行入口尚未保證拒絕重建前的衍生結果。`validated_roc_to_tx_yyyymm` 是明確保留的日期函式，不表示其他舊資料介面或結果可沿用。

## 目前能力與邊界

- `run-full` 在 `pipeline.py` 編排來源核對、離線階段、輸出階段及 run report，並支援階段續跑；這是現行實作事實，不授權續跑重建前快照。隔離護欄完成前不能用它安全地執行新重建。
- TGOS 使用獨立 CLI 命令及 `--work-dir`，尚未由 `run-full` 管理 TGOS 狀態或回補。
- `transactions/read.py` 目前只讀交易主檔。`_build.csv`、`_land.csv`、`_park.csv` 明細尚未連回主檔，新版公開輸出要求納入明細。
- `tgos/validate.py` 的 `import_tgos` 已檢查查詢與回傳集合一對一，並以相同批次和回傳雜湊支援冪等重匯。`addresses/identity.py` 的 `same_door` 允許回傳補上送出地址未寫的村里或鄰。R09-2 尚須檢查候選是否跨多個村里或鄰，未完成完整同址驗收。
- 現有 `output/` 實作舊格式及舊欄位契約。新 35 欄門牌輸出、CSV 34 欄、四種格式、`catalog.json` 和新版目錄尚未實作。權威規格在[輸出欄位](輸出欄位.md)。
- `config/sources/notice.json` 與本機 `data/output/` 目前不存在。gitignore 仍忽略整個 `data/`，部分 CLI 預設仍使用 `data/work/`。這些是尚待程式或設定工作處理的差距。
- R07-4 的 58 批舊契約 run report 曾標示完成，但其 `output-release` 已移除。該歷史結果及相關衍生資料不作目前接受的候選、定位依據或新版驗收基準。

## 相依資料與版本

來源清單、來源描述與小型規則放在 `config/`。原始 ZIP、TGOS 交換檔、執行快照、工作區及產物位置依命令與[資料處理流程](資料處理流程.md)管理。所有規則版本、輸入雜湊與程式版本都應能回連到單次 run 及其快照。

目前本機 run/stage 路徑仍有歷史差異。新版公開目錄與檔案契約只以[輸出欄位](輸出欄位.md)為準，其他目標布局依企劃工作實作後才視為現行可用。`data/output/` 是已決定的 Git 公開位置，但目前 `.gitignore` 仍忽略整個 `data/`，尚未成為實際追蹤輸出。檢查輸出及失敗診斷存入 `data/tmp/<purpose>/`。維護包留在本機執行目錄，不公開。

## 相關文件

- [使用、安裝及現行命令](../README.md)
- [資料處理流程](資料處理流程.md)
- [地址規則](地址規則.md)
- [內部資料契約](資料契約.md)
- [公開輸出契約](輸出欄位.md)
- [剩餘工作與驗收](plans/重建企劃.md)
