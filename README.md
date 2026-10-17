# taiwan-lvr-geodata

本專案把內政部實價登錄交易資料與地址定位證據整理成可追溯的地理資料。新版公開契約是一個有座標的門牌一列，提供 GeoParquet 與每月 CSV。GeoPackage、GeoJSON 與年度檔因檔案過大先不做。新版輸出程式已實作，尚未以真實資料執行與驗收，本機目前沒有 `data/output/`。

本文件是唯一使用入口，包含下載、安裝、來源準備、現有命令、TGOS、回補、發布與常見問題。模組責任見[架構](docs/architecture.md)，新版資料處理階段見[資料處理流程](docs/資料處理流程.md)，地址判定見[地址規則](docs/地址規則.md)，內部資料關聯見[資料契約](docs/資料契約.md)，公開欄位與目錄見[輸出欄位](docs/輸出欄位.md)。工作依賴與驗收狀態以[00 主企劃](docs/plans/00-主企劃.md)為準。

**2026-10-12 重建決定：所有既有衍生資料均不可信，不得用於新流程、定位或新版驗收。程式隔離護欄已實作，`run-full` 會拒絕舊快照、舊 run 及未核驗的來源清冊；舊 TGOS 結果與帳本已廢棄且本機不存在，新重建以新帳本重新送查。**

## 下載

第一版公開資料已於 2026-10-17 在本機 `data/output/` 建立：基線 `r07-10-20261016b` 的離線定位輸出，3,551,724 列，只含離線定位成功的門牌，TGOS 回補屬第二版。GitHub release 與推送由擁有者執行，完成前遠端可能還沒有這些檔案。檔案雜湊見 `data/output/catalog.json` 與[發布紀錄](docs/records/R08-3第一版發布.md)。2026-10-10 的 R07-4 全歷史執行曾完成舊輸出契約處理，但其輸出成品已移除。該次 V-09、V-10 pass 僅保留為歷史結果，不作目前接受或可沿用資料的依據。此處不判斷遠端是否仍有其他歷史 release。

舊資料與舊驗收不構成目前可接受的資料基準。既有 normalized／converted 結果、地址池／index／state、aliases、TGOS 採用結果、review 裁定、衍生 cache、快照、維護包、公開輸出及統計都須隔離。舊 V 的 pass 只保留為當時紀錄。不得拿舊統計或舊輸出自證來源完整或結果正確。

公開位置如下：

| 格式 | 位置 | 用途 |
| --- | --- | --- |
| GeoParquet | GitHub release 的 `taiwanlvr.parquet` | 程式與資料庫 |
| CSV | `data/output/csv/YYYY/taiwanlvr_YYYYMM_<類別>.csv`，每月每類別一檔 | 試算表、BI、QGIS、Kepler.gl 與 AI |

`data/output/catalog.json` 預定列出各檔案格式、年、月、類別、期間、筆數、大小、SHA-256 與路徑。完整契約見[輸出欄位](docs/輸出欄位.md)。上述目錄與欄位的產生、`catalog.json` 與格式間驗證已實作，全量驗收與發布尚未完成。

多門牌交易的一個總價屬於整筆交易，不分攤或推算。輸出列以 `door_count` 大於 1 表示多門牌，以 `transaction_id` 找出同筆交易的點，加總時依 `transaction_id` 去重。這些新欄位尚未由程式產生。每種格式的使用方式與加總規則將隨新版資料交付。

資料來源、顯名與限制見本節，不另設 NOTICE 檔。每版的變更內容寫在 GitHub release 說明。交易資料來自內政部不動產成交案件實際資訊資料供應系統，依政府資料開放授權條款第 1 版使用；門牌座標來自 `taiwan-address-data` 的固定提交。程式授權不代表第三方資料適用相同授權。

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

相關地址資料專案位於 `C:/Code/taiwan-address-data`，舊專案 `C:/Code/taiwan-lvr-geojson` 僅作唯讀參考。本機 `data/raw/` 與地址專案可隨時讀取。下載新來源須依擁有者指定的方式辦理，不自行推定來源權限，也不複製第三方地址來源列進 Git。

`verify-sources` 目前只執行程式已有的檔案、雜湊及 ZIP 結構檢查；它不是獨立的來源正確性或完整性證明。`run-full` 只接受帶 `rebuild_epoch: "2026-10-12"` 的原始清冊與地址來源描述。2026-10-13 擁有者決定取消人工簽核，以本次紀元重新掃描即為核驗，兩份清冊以 `scanned_at` 記錄掃描時間。`config/sources/raw_manifest.json` 與 `address_source.json` 已由 R07-6c 重新產生。

檢查設定和命令選項時，先查看實際入口：

```bash
uv run --locked --python 3.14.8 python -m lvr_pipeline --help
uv run --locked --python 3.14.8 python -m lvr_pipeline <command> --help
```

## 現有全量命令

`run-full` 是現行全量離線流程入口。它核對來源、執行離線階段、產生待查核資料、輸出及 run report。程式流程包含續跑與階段驗證。它目前不讀取 TGOS 狀態。輸出階段使用新版 22 欄公開契約，已由 R07-10 以真實資料執行，V-10b 通過。第一版輸出已由 R08-3 複製到 `data/output/`。

先核對來源清單與本機原始 ZIP：

```bash
uv run --locked --python 3.14.8 python -m lvr_pipeline verify-sources \
  --manifest config/sources/raw_manifest.json \
  --raw-dir data/raw
```

`run-full` 必須提供唯一 run ID、截止年月與地址資料目錄。不可指定舊 run ID、舊工作目錄或舊快照：

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

截止年月使用西元 `YYYYMM`。此為參數完整的命令模板，不是已驗收的發布命令。`run-full` 有來源核對、階段續跑、離線處理、新版 22 欄輸出及 run report，但未讀 TGOS 狀態。輸出寫在 run 目錄的 `output/<output_id>/public/` 與 `release/`，輸出階段會寫入 `catalog.json` 並執行格式間驗證，失敗時留下 `verify_report.json` 且不提交該階段。快照只在建立時逐列驗證一次，之後載入只核對雜湊；稽核時用 `python -m lvr_pipeline verify-snapshot --path <快照目錄>` 完整重驗。尚未有 `NOTICE.json`，也不是公開發布。舊的 `package-output`、`verify-output`、`backfill-output`、`fetch-output`、`publish-output`、`commit-release-pointer`、`verify-public-snapshot` 命令已於 2026-10-12 移除。單批轉換不能視為全歷史完成。流程輸入、每階段產物及失敗去向見[資料處理流程](docs/資料處理流程.md)。

明細讀入與關聯已於 2026-10-12 實作（R07-7）：`ingest` 讀入三種明細為 `ingest-detail`，`normalize` 依明細關聯契約以 `編號` 連回主檔，產生 `detail` 資料集，並把七項計數寫入 `detail_link`。新版公開輸出的三個 JSON 欄由 R07-8 產生。明細欄位與例外處理詳見[資料契約](docs/資料契約.md)及[輸出欄位](docs/輸出欄位.md)。

## TGOS 回補

TGOS 由個別命令處理，未接入 `run-full`。預定順序為 `prepare-tgos`、人工交換、`set-tgos-status`、`import-tgos`、`build-review`，之後新版回補由 R-10 定義。`prepare-tgos` 可從本次離線狀態建立初始 TGOS state，後續每個狀態更新都須使用前一命令輸出的新快照路徑。`build-review` 用 `--state` 接收離線狀態。該路徑須來自同一次 run。`prepare-tgos` 必須以 `--index` 指定同一 run 的 offline-index 快照，用於缺區地址的候選區。這五個命令都必須給 `--run-id`，狀態寫入 `data/runs/<run-id>/`；`--run-id` 是 run 目錄名，不是快照 ID。指向 `run-full` 的 run 目錄時命令拒絕執行。

舊 TGOS 帳本與舊回傳在本機已不存在，不轉接、不擋重送；新重建以新帳本從零開始，離線定位不到的門牌全部重新送查。`prepare-tgos --ledger` 只帶入本次重建紀元的帳本，舊紀元帳本一律拒絕；帳本中有現行規則算不出門牌鍵的查詢時也拒絕。新帳本在重建期間不得清空或繞過，`submission_unknown` 不得自動重送。

首輪送查依 [00 主企劃 C 段](docs/plans/00-主企劃.md) 的 R09-7 執行，R09-6 完成前不開始。每檔最多 10,000 筆是單檔上限，不是每日程式限制。未知送出狀態必須保留，阻止重送或釋放配額。以下命令僅查詢介面：

```bash
uv run --locked --python 3.14.8 python -m lvr_pipeline prepare-tgos --help
uv run --locked --python 3.14.8 python -m lvr_pipeline set-tgos-status --help
uv run --locked --python 3.14.8 python -m lvr_pipeline import-tgos --help
uv run --locked --python 3.14.8 python -m lvr_pipeline build-review --help
```

目前沒有回補命令。新版回補由 R-10 依本次全新重建且已驗收的基線重新實作，不使用重建前的狀態或公開輸出。

現行 TGOS 匯入會檢查查詢與回傳集合一對一，並具備相同批次及相同回傳雜湊的冪等重匯處理；已完成批次的不同回傳會被拒絕。`same_door` 允許回傳補上送出地址未寫的村里或鄰；同一門牌在本次地址池中有兩個以上不同村里或鄰時不採用，記 `tgos_village_ambiguous`（R09-2）。完整同址的真實回傳驗收由 V-12 判定。座標軸不明、門牌不符或證據不足時不得採用。

送查、回傳原檔及帳本屬執行資料。狀態保存在 `data/runs/<run-id>/tgos-state/`，交換檔預設在 `data/tgos/`。不要覆寫原始回傳或重設配額帳本。

## 歷史回補與發布

只有經新規則核驗、明確允許納入的 TGOS 證據，才能建立本次新定位狀態並回補本次重建的來源觀測。舊定位、審查裁定、快照與輸出不得回補或作為定位依據。新版回補仍待 R-10 實作。地址專案新增或更新候選、真實匯入及新版公開資料發布分別依企劃中的工作與驗收條件處理。

發布前要完成候選驗證、附件清單與雜湊核對。公開發布後再下載公開檔案，核對雜湊及版本內容。維護包只存本機，不公開。公開輸出 `data/output/` 是已決定的 Git 下載位置；GeoParquet 單檔由 GitHub release 提供。`.gitignore` 已於 R07-9 改為忽略 `data/*` 但不忽略 `data/output/`，所以 `data/output/` 下的檔案會被 Git 追蹤；只有經發布流程核准的公開輸出應放進該目錄。各步驟尚未完成前，不要更新公開指標。更完整的開發及發布限制見[CONTRIBUTING](CONTRIBUTING.md)。

## 常見問題

### 為什麼沒有下載連結？

本機檢查未找到新版可下載成品；舊輸出已移除，新版輸出程式已實作但尚未以真實資料執行和驗收。本次未核對遠端 release。

### `run-full` 成功代表新版資料已發布嗎？

不是。命令完成只表示該次程式流程產生 run report。新版輸出、公開候選驗證、發布及發布後下載核對有各自的驗收條件。

### 為什麼未定位地址沒有出現在下載資料？

新版公開規格只收錄有座標的門牌。未定位資料留在內部待查核資料及 TGOS 候選池，不會以空幾何列進入公開輸出。

### 多門牌交易可以直接加總列上的總價嗎？

不可以直接加總所有門牌列。同一交易各列保留整筆交易總價，應先依 `transaction_id` 去重再加總。

### 文件中的舊驗收數字還有效嗎？

歷史紀錄只保留當時字面結果，不是目前資料可信度或驗收通過依據。所有既有 V 均須按新版範圍重新取得證據；新版 22 欄公開契約的程式已實作，尚待以真實資料執行及驗收。

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
| 工作依賴、V 驗收與待決實作契約 | [00 主企劃](docs/plans/00-主企劃.md) |
| 舊版命令、規格與原始企劃 | `docs/archive/` 已於 2026-10-18 由擁有者刪除，原文在 git 歷史，提交 `c3f1284` 之前 |
