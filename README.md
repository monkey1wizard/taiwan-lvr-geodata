# taiwan-lvr-geodata

本專案保留台灣實價登錄資料的解析與地址處理程式，作為新版地理資料管線的起點。

P0 基礎程式及固定套件環境已實作，Windows 與 GitHub Ubuntu 均通過 145 個測試。已實作內容、CI 證據與限制見 [P0 紀錄](docs/p0-foundations.md)。P0 已整合至 main。本機正式作業目錄為 `C:/Code/taiwan-lvr-geodata`，P0 檔案已完整同步。

P1 已在正式目錄實作逐批轉換命令與內部 Parquet 快照。Windows 與 GitHub Ubuntu 各 178 個測試，以及真實 `115q1` 批次已通過驗證，詳見 [P1 操作與證據](docs/p1-conversion.md)。P1 已直接整合至 main，先前 PR #2 不再作為交付流程。

P2 已完成離線地址池、定位、GIS 月檔、年度 ZIP 與 GitHub 公開交接。Windows／Ubuntu 各 211 個測試通過。全新 Linux 未提供 raw，重產的 783 個月檔雜湊全部相同。首版 [p2-115q1-offline-v2](https://github.com/monkey1wizard/taiwan-lvr-geodata/releases/tag/data-p2-115q1-offline-v2) 有 102,743 筆來源觀測、87 個交易月份與 10 個年度，座標來源限官方臺北市資料。各月／年皆為 `scope_limited`，其餘縣市的未定位資料仍保留。見 [P2 驗收與命令](docs/p2-offline-output.md)及 [下載說明](docs/downloads.md)。

新版方案見[完整企劃草案](docs/drafts/taiwan-lvr-geodata-完整企劃.md)，內含六個 phases、24 個 tasks 與 26 個 test points，可直接作為 cloud agent 的工作依據。修訂後的執行設計已通過獨立審查。各 task 依自己的前置條件與驗收執行，不要求完整 GAL 流程。

P1 建立內部 Parse／Normalize 階段快照。P2 完成離線地址處理後，先公開交付 output，後續 TGOS 再回補。下載以交易月份 `tx_yyyymm` 為最小時間單位，保留 `YYYYMM_category` 檔名，另提供依格式打包的年度 ZIP。使用者可只下載需要的月份／類別／格式，也可下載整年。

月輸出、年度包及維護狀態已保存於不可變 Release，約 582.4 MiB。Git 只保存程式、來源描述及小型發布指標。cloud agent 已可取得維護包並重產離線 output。人工程序沿用 addrCompare 每日／每片最多 10,000 筆與 WGS84；地址 repo 回饋仍待 P4。

P3 已完成持久 TGOS 配額／批次狀態、UTF-8-sig 人工交換、嚴格回傳匯入及受影響月／年回補程式。Windows 與 GitHub Ubuntu 各 218 個測試通過。最新 `taiwan-address-data` commit `02887978…` 的真實離線重建留下 4,871 個 TGOS 候選；真實批次需先確認共用帳號當日外部已用筆數，尚未上傳或回補發布。見 [P3 執行證據](docs/p3-evidence.json)與 [cloud 操作](docs/cloud-runbook.md)。

## 本機開發與 Git

正式目錄為 `C:/Code/taiwan-lvr-geodata`，在 `main` 修改、測試及 commit，再直接 push 至 `origin/main`。本機與遠端 main 保持同步。後續工作不另建工作副本、階段分支或 PR。GitHub Actions 用於 Linux 驗證。

## 目前內容

- `lvr_pipeline/0_parse_raw.py`：讀取原始 ZIP，分流買賣、預售屋與租賃，另存土地與純車位。
- `lvr_pipeline/1_normalize.py`：套用人工補字規則，產生乾淨地址與待補字地址清單。
- `lvr_pipeline/address.py`：地址解析、正規化與 `building_key()`。
- `lvr_pipeline/garbled.py`、`garbled_resolve.py`：人工缺字補正與路名候選處理。
- `lvr_pipeline/tx_date.py`：民國日期轉交易年月。
- `lvr_pipeline/ingest.py`、`normalize.py`、`converted.py`：P1 的逐批解析、來源觀測正規化及內部轉換快照。
- `lvr_pipeline/parquet_io.py`、`snapshots.py`：型別、筆數、雜湊、關聯與提交復原檢查。
- `lvr_pipeline/offline_lookup.py`、`address_pool.py`、`address_state.py`：固定離線索引、全域池與獨立來源關聯，以及唯一／衝突／未定位狀態。
- `lvr_pipeline/export.py`、`packaging.py`、`distribution.py`：三格式月檔、原月檔年度 ZIP、維護包、公開取得／驗證及預期 parent 指標提交。
- `lvr_pipeline/tgos.py`、`backfill.py`：P3 配額保留、人工狀態、回傳匯入、別名事件與受影響月／年回補。
- `tests/`：既有解析／補字測試，以及 P0～P3 的合成驗收測試。
- `data/registry/garbled_override.csv`：既有人工補字規則。
- `data/reference/`：路名參考資料與來源紀錄。

`0_parse_raw` 與 `1_normalize` 保留舊 CSV 格式。新版逐批轉換請使用下方 P1 命令。

## 使用 P1 逐批轉換

將已驗證的 raw 放入 `data/raw/`，從 repo 根目錄執行。資料範圍與 cutoff 必須明確，run ID 使用未占用的名稱。

```powershell
uv run --locked --python 3.13.16 python -m lvr_pipeline export-converted --batch 115q1 --cutoff 202610 --run-id my-115q1-run
uv run --locked --python 3.13.16 python -m lvr_pipeline verify-converted --input data/work/converted/snapshots/my-115q1-run
```

輸出仍是離線定位前的內部資料，不提供使用者 GIS 月／年下載。來源異常、排除記錄與未解地址都保留供核對。逐階段操作、重跑規則與驗收範圍見 [P1 文件](docs/p1-conversion.md)。

## 執行測試

既有解析與補字程式只使用 Python 標準函式庫。P0 使用 jsonschema、PyArrow、DuckDB 與 pytest，完整依賴固定於 uv.lock。固定環境及 Linux setup 指令見 [P0 紀錄](docs/p0-foundations.md)。

在專案根目錄執行：

```powershell
uv sync --locked --group dev --python 3.13.16
uv run --locked --python 3.13.16 python scripts/build_fixtures.py
uv run --locked --python 3.13.16 python -m pytest -q
```

## 使用既有解析與補字程式

先依[資料來源說明](docs/DATA_SOURCES.md)將原始 ZIP 放入 `data/raw/`。程式不會自動讀取 `.env`，請透過執行環境設定需要的變數。

```powershell
python -m lvr_pipeline.0_parse_raw 115q1
python -m lvr_pipeline.1_normalize
```

第一個指令選取 `115q1` 批次。第二個指令處理所有已產生的 category 檔案，也可接受 `sales`、`presale` 或 `rent` 選取單一類別。它不接受季度作為篩選條件。

解析程式會覆寫 `data/work/meta_*.csv`、`data/registry/land.csv` 與 `parking.csv`。補字程式會直接更新 meta CSV，並將地址清單寫入 `data/work/offline_in/`。這些結果皆不納入 Git。

## 文件狀態

- [完整企劃草案](docs/drafts/taiwan-lvr-geodata-完整企劃.md)：離線首版 output、月／年下載、GitHub 公開交接及完整 phases／tasks／test points。
- [task 結果範本](docs/task-result-template.md)：agent 記錄前置版本、命令、測試、輸出雜湊及交接位置。
- [完整資料處理流程](docs/data-processing-flow.md)：九個子流程，含已實作 P3 路徑與仍待人工執行的邊界。
- [P3 執行證據](docs/p3-evidence.json)：最新地址來源、真實離線重建、測試與 TGOS 未執行狀態。
- [P2 驗收](docs/p2-offline-output.md)：已發布首版、實測資源、測試與已實作命令。
- [cloud 操作](docs/cloud-runbook.md)：取得維護包、固定離線地址來源、建立 TGOS 批次、匯入與回補。
- [發布操作](docs/release-runbook.md)：draft 傳送核對、不可變 Release 與 main 指標提交。
- [新版資料處理流程草稿](docs/drafts/taiwan-lvr-geodata-新版資料處理流程.md)：Google Drive 文件的完整內容快照，後續將重寫。
- [舊文件參考索引](docs/legacy/README.md)：保留可再利用的舊文件原文，列出已知過時內容。
- [資料來源](docs/DATA_SOURCES.md)：本次搬移所需的來源與本機資料放置方式。

舊 SQLite 程式、定位成果、一次性回灌工具、原始 ZIP 與工作資料沒有搬入本 repo。原專案仍可作為後續改寫參考。

## 來源與授權

程式碼與既有測試複製自 `taiwan-lvr-geojson`。來源 Git HEAD 為 `5be26dc3e8b3a6425abb9b56f8fe4b9d9f8e187c`，搬移日期為 2026-10-04。

程式碼授權見 [LICENSE](LICENSE)。路名資料的來源與授權紀錄見 [provenance](data/reference/roadnames_35321.provenance.txt)。第三方門牌座標資料未包含在本 repo。
