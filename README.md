# taiwan-lvr-geodata

本專案把內政部實價登錄交易資料與地址定位證據整理成可追溯的地理資料。新版公開契約是一個有座標的門牌一列，提供 GeoParquet、GeoPackage、CSV 與 GeoJSON。新版輸出尚未實作，本機目前沒有 `data/output/`。

本文件是唯一使用入口，包含下載、安裝、來源準備、現有命令、TGOS、回補、發布與常見問題。模組責任見[架構](docs/architecture.md)，新版資料處理階段見[資料處理流程](docs/資料處理流程.md)，地址判定見[地址規則](docs/地址規則.md)，內部資料關聯見[資料契約](docs/資料契約.md)，公開欄位與目錄見[輸出欄位](docs/輸出欄位.md)。工作依賴與驗收狀態以[重建企劃](docs/plans/重建企劃.md)為準。

**2026-10-12 重建決定：所有既有衍生資料均不可信，不得用於新流程、定位或新版驗收。程式隔離護欄已實作，`run-full` 會拒絕舊快照、舊 run 及未核驗的來源清冊；舊公開輸出、維護包與 TGOS 帳本轉接仍不在護欄範圍。**

## 下載

本機目前沒有新版公開資料可下載。2026-10-10 的 R07-4 全歷史執行曾完成舊輸出契約處理，但其輸出成品已移除。該次 V-09、V-10 pass 僅保留為歷史結果，不作目前接受或可沿用資料的依據。此處不判斷遠端是否仍有其他歷史 release。

舊資料與舊驗收不構成目前可接受的資料基準。既有 normalized／converted 結果、地址池／index／state、aliases、TGOS 採用結果、review 裁定、衍生 cache、快照、維護包、公開輸出及統計都須隔離。舊 V 的 pass 只保留為當時紀錄。不得拿舊統計或舊輸出自證來源完整或結果正確。

新版完成後，公開位置預定如下：

| 格式 | 位置 | 用途 |
| --- | --- | --- |
| GeoParquet | GitHub release 的 `taiwanlvr.parquet` | 程式與資料庫 |
| GeoPackage | `data/output/gpkg/taiwanlvr_YYYY.gpkg` | QGIS、ArcGIS |
| CSV | `data/output/csv/YYYY/` | 試算表、BI、Kepler.gl 與 AI |
| GeoJSON | `data/output/geojson/YYYY/` | 網頁地圖 |

`data/output/catalog.json` 預定列出各檔案格式、年、月、類別、期間、筆數、大小、SHA-256 與路徑。完整契約見[輸出欄位](docs/輸出欄位.md)。上述目錄與欄位是已決定的新版規格，實作及驗收尚未完成。

多門牌交易的一個總價屬於整筆交易，不分攤或推算。輸出列以 `door_count` 大於 1 表示多門牌，以 `transaction_id` 找出同筆交易的點，加總時依 `transaction_id` 去重。這些新欄位尚未由程式產生。每種格式的使用方式與加總規則將隨新版資料交付。

資料來源、顯名與限制以每版 `NOTICE.json` 為準。程式授權不代表第三方資料適用相同授權。

## 安裝與測試

環境使用 Python 3.14.8 與 uv 0.12.23，並安裝鎖定的相依套件。CI 只驗證 Linux 的 `scripts/setup.sh`；Windows 命令未由 CI 驗證。Linux 已安裝指定版本 uv 後，在專案根目錄執行：

```bash
bash scripts/setup.sh
```

Windows PowerShell 可用固定版 uv 執行以下合成 fixture 與測試命令，uv 會依專案要求取得 Python 3.14.8；此 Windows 路徑目前沒有 CI 驗證，也不保證可用：

```powershell
uv sync --locked --group dev --python 3.14.8
uv run --locked --python 3.14.8 python scripts/build_fixtures.py
uv run --locked --python 3.14.8 python -m pytest -q
```

安裝與自動測試只使用合成輸入，不下載真實資料，也不需要憑證。測試範圍及提交、文件同步規範見[CONTRIBUTING](CONTRIBUTING.md)。

## 準備來源

新 run 必須從重新核對的來源清單、原始 ZIP 與地址來源重新建立。現有清單、輸入檔及其雜湊尚未因此被證明完整、真實或正確；需獨立核對來源涵蓋、原文與內容。不可由舊盤點、舊 run report、舊轉換或定位結果自證正確，也不可由缺檔推定為已知空批次。衍生 cache、快照及舊輸出不得作為新流程輸入。

| 輸入 | 位置或描述檔 | 準備要求 |
| --- | --- | --- |
| 交易批次清單 | `config/sources/raw_manifest.json` | 清單列出已盤點批次，仍須逐批核對實體檔及 ZIP 成員 |
| 原始交易 ZIP | `data/raw/<batch>_lvr_landcsv.zip` | 依清單核對 SHA-256 與 ZIP 內容 |
| 地址來源 | `config/sources/address_source.json` | 固定地址專案提交及檔案，不以最新版本代替固定版本 |
| 臺北市官方來源 | `config/sources/official_taipei.json` | 使用描述所列來源檔與雜湊 |
| 補字規則 | `config/rules/character-fixes.csv` | 執行時固定規則版本。適用規則見[地址規則](docs/地址規則.md)；[R-04紀錄](docs/records/R04執行紀錄.md)只保存歷史案例結果，新規則須獨立驗證 |
| 路名參考 | `config/reference/roadnames_35321.provenance.txt` | 保留來源及適用限制 |

相關地址資料專案位於 `C:/Code/taiwan-address-data`，舊專案 `C:/Code/taiwan-lvr-geojson` 僅作唯讀參考。取得真實來源須依擁有者明確授權的操作方式辦理，不自行推定來源權限或複製第三方地址來源列進 Git。

`verify-sources` 目前只執行程式已有的檔案、雜湊及 ZIP 結構檢查；它不是獨立的來源正確性或完整性證明。`run-full` 只接受帶 `rebuild_epoch: "2026-10-12"` 且 `source_check.status` 為 `verified` 的原始清冊與地址來源描述。程式重新產生的清冊預設為 `pending`；擁有者核對來源頁面、授權與下載紀錄後，填入 `checked_by`、`checked_at`、`evidence` 並改為 `verified`。現有 `config/sources/raw_manifest.json` 與 `address_source.json` 是舊清冊，會被拒絕。

檢查設定和命令選項時，先查看實際入口：

```bash
uv run --locked --python 3.14.8 python -m lvr_pipeline --help
uv run --locked --python 3.14.8 python -m lvr_pipeline <command> --help
```

## 現有全量命令

`run-full` 是現行全量離線流程入口。它核對來源、執行離線階段、產生待查核資料、輸出及 run report。程式流程包含續跑與階段驗證。它目前不讀取 TGOS 狀態，也未實作新版公開輸出契約。`data/output/` 及公告檔 `config/sources/notice.json` 目前不存在，因此尚不能執行新版全量交付。

先核對來源清單與本機原始 ZIP：

```bash
uv run --locked --python 3.14.8 python -m lvr_pipeline verify-sources \
  --manifest config/sources/raw_manifest.json \
  --raw-dir data/raw
```

`run-full` 必須提供唯一 run ID、截止年月與地址資料目錄。原始清冊與地址來源描述經擁有者核驗為 `verified` 之前，以下模板會被拒絕。不可指定舊 run ID、舊工作目錄或舊快照：

```bash
uv run --locked --python 3.14.8 python -m lvr_pipeline run-full \
  --run-id <new-run-id> \
  --cutoff <YYYYMM> \
  --address-dir <fixed-address-data-directory> \
  --manifest config/sources/raw_manifest.json \
  --raw-dir data/raw \
  --address-source config/sources/address_source.json \
  --garbled-rules config/rules/character-fixes.csv \
  --config config/pipeline.example.toml
```

截止年月使用西元 `YYYYMM`。此為參數完整的命令模板，不是已驗收的發布命令。`run-full` 有來源核對、階段續跑、離線處理、新版 22 欄輸出及 run report，但未讀 TGOS 狀態。輸出寫在 run 目錄的 `output/<output_id>/public/` 與 `release/`，尚未有 `catalog.json`、`NOTICE.json` 或格式間驗證（R07-9），也不是公開發布。舊的 `package-output`、`verify-output`、`backfill-output`、`fetch-output`、`publish-output`、`commit-release-pointer`、`verify-public-snapshot` 命令已於 2026-10-12 移除。單批轉換不能視為全歷史完成。流程輸入、每階段產物及失敗去向見[資料處理流程](docs/資料處理流程.md)。

明細讀入與關聯已於 2026-10-12 實作（R07-7）：`ingest` 讀入三種明細為 `ingest-detail`，`normalize` 依明細關聯契約以 `編號` 連回主檔，產生 `detail` 資料集，並把七項計數寫入 `detail_link`。新版公開輸出的三個 JSON 欄由 R07-8 產生。明細欄位與例外處理詳見[資料契約](docs/資料契約.md)及[輸出欄位](docs/輸出欄位.md)。

## TGOS 回補

TGOS 由個別命令處理，未接入 `run-full`。預定順序為 `prepare-tgos`、人工交換、`set-tgos-status`、`import-tgos`、`build-review`，之後新版回補由 R-10 定義。`prepare-tgos` 可從本次離線狀態建立初始 TGOS state，後續每個狀態更新都須使用前一命令輸出的新快照路徑。`build-review` 用 `--state` 接收離線狀態。該路徑須來自同一次 run。

目前沒有按新 `building_key` 契約核驗過的安全帳本轉接步驟。現行 `--ledger` 會轉接舊送查記錄，並以 `building_key` 重新生成查詢鍵與指紋；無法重鍵的查詢會保留診斷資料，不能當成新防重送索引。此行為尚未按新契約驗收。轉接會丟棄舊 results、evidence 與 aliases，因此不得用舊定位結果。R07-6a/R09 須保留送查事實、核對歷史送出狀態並建立新的防重送索引，不繼承舊鍵、定位、aliases 或裁定。不得清空帳本、忽略未知送出狀態或重送。舊 TGOS 回傳預設排除，僅在新規則核對回傳原文同址並明確允許後才能納入。R07-6a 護欄不涵蓋 TGOS 帳本轉接，R09-2 完成前以下只可查詢命令參數：

目前只可查看命令參數，不能依此執行新重建 TGOS 流程。每檔最多 10,000 筆是單檔上限，不是每日程式限制。未知送出狀態必須保留，阻止重送或釋放配額。以下命令僅查詢介面：

```bash
uv run --locked --python 3.14.8 python -m lvr_pipeline prepare-tgos --help
uv run --locked --python 3.14.8 python -m lvr_pipeline set-tgos-status --help
uv run --locked --python 3.14.8 python -m lvr_pipeline import-tgos --help
uv run --locked --python 3.14.8 python -m lvr_pipeline build-review --help
```

目前沒有回補命令。新版回補由 R-10 依本次全新重建且已驗收的基線重新實作，不使用重建前的狀態或公開輸出。

現行 TGOS 匯入會檢查查詢與回傳集合一對一，並具備相同批次及相同回傳雜湊的冪等重匯處理。`same_door` 允許回傳補上送出地址未寫的村里或鄰。R09-2 尚待補足候選跨村里／鄰歧義核對，完整同址驗收仍未通過。座標軸不明、門牌不符或證據不足時不得採用。

送查、回傳原檔及帳本屬執行資料。依實際命令提供 `--work-dir`，保存在指定 run 或 `data/tmp/` 工作區。不要覆寫原始回傳或重設配額帳本。

## 歷史回補與發布

只有經新規則核驗、明確允許納入的 TGOS 證據，才能建立本次新定位狀態並回補本次重建的來源觀測。舊定位、審查裁定、快照與輸出不得回補或作為定位依據。新版回補仍待 R-10 實作。地址專案新增或更新候選、真實匯入及新版公開資料發布分別依企劃中的工作與驗收條件處理。

發布前要完成候選驗證、正式公告確認、附件清單與雜湊核對。公開發布後再下載公開檔案，核對雜湊及版本內容。維護包只存本機，不公開。公開輸出 `data/output/` 是已決定的 Git 下載位置；GeoParquet 單檔由 GitHub release 提供。目前 `.gitignore` 仍忽略整個 `data/`，輸出追蹤尚未實作。各步驟尚未完成前，不要更新公開指標。更完整的開發及發布限制見[CONTRIBUTING](CONTRIBUTING.md)。

## 常見問題

### 為什麼沒有下載連結？

本機檢查未找到新版可下載成品；舊輸出已移除，新輸出契約尚未實作和驗收。本次未核對遠端 release。

### `run-full` 成功代表新版資料已發布嗎？

不是。命令完成只表示該次程式流程產生 run report。新版輸出、來源公告、公開候選驗證、發布及發布後下載核對有各自的驗收條件。

### 為什麼未定位地址沒有出現在下載資料？

新版公開規格只收錄有座標的門牌。未定位資料留在內部待查核資料及 TGOS 候選池，不會以空幾何列進入公開輸出。

### 多門牌交易可以直接加總列上的總價嗎？

不可以直接加總所有門牌列。同一交易各列保留整筆交易總價，應先依 `transaction_id` 去重再加總。

### 文件中的舊驗收數字還有效嗎？

歷史紀錄只保留當時字面結果，不是目前資料可信度或驗收通過依據。所有既有 V 均須按新版範圍重新取得證據；新版 22 欄公開契約尚待實作及驗收。

## 文件導覽

| 主題 | 文件 |
| --- | --- |
| 執行入口與使用說明 | 本頁 |
| 開發、測試、提交與文件同步 | [CONTRIBUTING](CONTRIBUTING.md) |
| 現行模組責任、相依方向與專案邊界 | [架構](docs/architecture.md) |
| 實際資料處理階段及等待／失敗去向 | [資料處理流程](docs/資料處理流程.md) |
| 地址補字、拆解、同址、座標與 TGOS 規則 | [地址規則](docs/地址規則.md) |
| 內部來源觀測、成員、鍵、證據、狀態及快照 | [資料契約](docs/資料契約.md) |
| 新版公開欄位、檔案映射與目錄 | [輸出欄位](docs/輸出欄位.md) |
| 工作依賴、V 驗收與待決實作契約 | [重建企劃](docs/plans/重建企劃.md) |
| 舊版命令、規格與原始企劃 | [歷史文件](docs/archive/) |
