# taiwan-lvr-geodata

本專案保留台灣實價登錄資料的解析與地址處理程式，作為新版地理資料管線的起點。

P0 基礎程式及固定套件環境已實作，Windows 與 GitHub Ubuntu 均通過 145 個測試。已實作內容、CI 證據與限制見 [P0 紀錄](docs/p0-foundations.md)。P0 已整合至 main。本機正式作業目錄為 `C:/Code/taiwan-lvr-geodata`，P0 檔案已完整同步。

P1 已在正式目錄實作逐批轉換命令與內部 Parquet 快照。Windows 與 GitHub Ubuntu 各 178 個測試，以及真實 `115q1` 批次已通過驗證，詳見 [P1 操作與證據](docs/p1-conversion.md)。P1 已直接整合至 main，先前 PR #2 不再作為交付流程。

P2 已完成離線地址池、定位、GIS 月檔、年度 ZIP 與 GitHub 公開交接。Windows／Ubuntu 各 211 個測試通過。全新 Linux 未提供 raw，重產的 783 個月檔雜湊全部相同。首版 [p2-115q1-offline-v2](https://github.com/monkey1wizard/taiwan-lvr-geodata/releases/tag/data-p2-115q1-offline-v2) 有 102,743 筆來源觀測、87 個交易月份與 10 個年度，座標來源限官方臺北市資料。各月／年皆為 `scope_limited`，其餘縣市的未定位資料仍保留。見 [P2 驗收與命令](docs/p2-offline-output.md)及 [下載說明](docs/downloads.md)。

新版方案見[完整企劃草案](docs/drafts/taiwan-lvr-geodata-完整企劃.md)，內含六個 phases、24 個 tasks 與 26 個 test points，可直接作為 cloud agent 的工作依據。修訂後的執行設計已通過獨立審查。各 task 依自己的前置條件與驗收執行，不要求完整 GAL 流程。

P1 建立內部 Parse／Normalize 階段快照。P2 完成離線地址處理後，先公開交付 output，後續 TGOS 再回補。下載以交易月份 `tx_yyyymm` 為最小時間單位，保留 `YYYYMM_category` 檔名，另提供依格式打包的年度 ZIP。使用者可只下載需要的月份／類別／格式，也可下載整年。

月輸出、年度包及維護狀態已保存於不可變 Release，約 582.4 MiB。Git 只保存程式、來源描述及小型發布指標。cloud agent 已可取得維護包並重產離線 output。人工程序沿用 addrCompare 每日／每片最多 10,000 筆與 WGS84；地址 repo 回饋仍待 P4。

P3 已完成持久 TGOS 配額／批次狀態、UTF-8-sig 人工交換、嚴格回傳匯入及受影響月／年回補程式。Windows 與 GitHub Ubuntu 各 219 個測試通過。最新 `taiwan-address-data` commit `02887978…` 的乾淨版本真實離線重建留下 4,871 個 TGOS 候選。第一個真實批次 `p3-tgos-20261005-001` 已送出，目前等待 TGOS 回傳。見 [TGOS 操作手冊](docs/tgos-runbook.md)、[P3 執行證據](docs/p3-evidence.json)與 [cloud 操作](docs/cloud-runbook.md)。

## 本機開發與 Git

正式目錄為 `C:/Code/taiwan-lvr-geodata`，在 `main` 修改、測試及 commit，再直接 push 至 `origin/main`。本機與遠端 main 保持同步。後續工作不另建工作副本、階段分支或 PR。GitHub Actions 用於 Linux 驗證。

## 資料夾結構

下列 tree 同時列出 Git 追蹤內容與程式執行時使用的本機目錄。標成
`[local]` 的目錄由 `.gitignore` 排除，不會隨 clone 取得，也不能提交。

```text
taiwan-lvr-geodata/
├── .github/
│   └── workflows/
│       ├── ci.yml                    push／PR 的 Linux 合成測試
│       └── verify-public-output.yml  手動驗證公開維護快照
├── config/
│   └── pipeline.example.toml         管線設定範例
├── data/
│   ├── raw/                          [local] 原始實價登錄 ZIP
│   ├── work/                         [local] 階段快照與量測結果
│   ├── cache/                        [local] 已核對的下載快取
│   ├── output/                       [local] 月檔、年包發布候選
│   ├── downloads/                    [local] 公開維護包下載內容
│   ├── tgos/                         [local] TGOS 人工交換 CSV／manifest
│   ├── reference/
│   │   ├── roadnames_35321_20260618.csv
│   │   └── roadnames_35321.provenance.txt
│   ├── registry/
│   │   └── garbled_override.csv      已確認的缺字補正規則
│   ├── releases/
│   │   ├── latest.json               最新公開版本指標
│   │   └── p2-115q1-offline-v2.json  P2 固定版本指標
│   └── sources/
│       ├── raw_manifest.json         58 批 raw 來源清單
│       ├── address_source.json       固定地址 repo 版本與檔案雜湊
│       ├── official_taipei.json      臺北市官方地址來源描述
│       └── p2_notice.json            P2 發布範圍與權利聲明
├── docs/
│   ├── drafts/
│   │   ├── taiwan-lvr-geodata-完整企劃.md
│   │   └── taiwan-lvr-geodata-新版資料處理流程.md
│   ├── legacy/
│   │   ├── README.md                 舊文件適用範圍索引
│   │   ├── CONTRACT.md               舊資料契約參考
│   │   ├── DATA_SOURCES.md           舊來源說明參考
│   │   ├── PROCESSING.md             舊處理流程參考
│   │   └── RESUBMIT_RUNBOOK.md       舊 TGOS 操作條件
│   ├── DATA_SOURCES.md               現行來源與本機放置方式
│   ├── data-contract.md              P2／P3 Parquet 與狀態契約
│   ├── data-processing-flow.md       九個完整處理流程圖
│   ├── downloads.md                  月檔／年度包下載說明
│   ├── cloud-runbook.md              cloud 接續 P3 操作
│   ├── tgos-runbook.md               TGOS 上傳、回傳、匯入與回補操作
│   ├── release-runbook.md            GitHub Release 發布／復原
│   ├── p0-foundations.md             P0 驗收紀錄
│   ├── p1-conversion.md              P1 操作與驗收紀錄
│   ├── p2-offline-output.md          P2 操作與驗收紀錄
│   ├── p1-evidence.json              P1 機器可讀證據
│   ├── p2-evidence.json              P2 機器可讀證據
│   ├── p3-evidence.json              P3 機器可讀證據
│   └── task-result-template.md       task 命令／結果紀錄
├── lvr_pipeline/
│   ├── __init__.py                   Python package 標記
│   ├── __main__.py                   `python -m lvr_pipeline` 入口
│   ├── cli.py                        P1 命令與總命令路由
│   ├── p2_cli.py                     P2／P3 命令與參數
│   ├── 0_parse_raw.py                保留的舊版 raw CSV 解析入口
│   ├── 1_normalize.py                保留的舊版 CSV 補字入口
│   ├── ingest.py                     P1 ZIP／CSV 逐批讀取
│   ├── normalize.py                  P1 觀測、地址及日期正規化
│   ├── converted.py                  P1 converted 快照輸出
│   ├── address.py                    舊入口相容地址函式
│   ├── address_v2.py                 `building_key_v2` 地址識別
│   ├── garbled.py                    缺字偵測與補正規則
│   ├── garbled_resolve.py            路名候選的唯一性驗證
│   ├── tx_date.py                    民國日期轉交易年月
│   ├── contracts.py                  P0／P1 JSON Schema 驗證
│   ├── p2_contracts.py               P2／P3 Arrow schema
│   ├── parquet_io.py                 Parquet 寫入與關聯驗證
│   ├── processing.py                 階段 binding、雜湊與資源量測
│   ├── snapshots.py                  不可變快照提交／復原
│   ├── sources.py                    來源檔案與 SHA-256 工具
│   ├── address_source.py             固定地址 repo 描述檔
│   ├── offline_lookup.py             離線門牌索引建立／查詢
│   ├── address_pool.py               唯一地址池與來源關聯
│   ├── address_state.py              離線定位及衝突狀態
│   ├── export.py                     月 GeoParquet／GeoJSON／NDJSON
│   ├── packaging.py                  月檔、年度 ZIP、維護包
│   ├── distribution.py               公開取得、Release 與版本指標
│   ├── tgos.py                       P3 配額、批次、匯入與別名事件
│   └── backfill.py                   P3 受影響月份／年度回補
├── schemas/
│   ├── observation.schema.json       來源觀測契約
│   ├── address-component.schema.json 地址成員契約
│   ├── exclusion.schema.json         排除結果契約
│   ├── diagnostic.schema.json        診斷契約
│   └── converted-parquet.json        converted 資料集契約
├── scripts/
│   ├── setup.sh                      Linux 固定安裝與完整測試
│   ├── build_fixtures.py             產生合成 ZIP／CSV fixtures
│   ├── inventory_sources.py          盤點 raw 與地址來源
│   └── verify_public_snapshot.py     無 raw 重產公開月檔
├── tests/
│   ├── fixtures/p0/
│   │   ├── addresses.csv             小型合成門牌資料
│   │   ├── 101q1_lvr_landcsv.zip     已知空批次 fixture
│   │   └── 115q1_lvr_landcsv.zip     合成交易 fixture
│   ├── test_0_parse_raw.py           舊解析入口相容測試
│   ├── test_1_normalize.py           舊補字入口相容測試
│   ├── test_address.py               舊地址函式測試
│   ├── test_garbled.py               缺字規則測試
│   ├── test_garbled_resolve.py       候選消歧測試
│   ├── test_tx_date.py               民國日期測試
│   ├── test_p0_smoke.py              P0 安裝與小型整合
│   ├── test_p0_contracts.py          P0 JSON Schema 契約
│   ├── test_p0_parquet.py            P0 Parquet 型別／關聯
│   ├── test_p0_snapshots.py          P0 快照提交／復原
│   ├── test_p0_sources.py            P0 來源描述與雜湊
│   ├── test_p1_conversion.py         P1 逐批轉換整合
│   ├── test_p1_resources.py          P1 資源量測
│   ├── test_p1_validation.py         P1 拒絕與關聯驗證
│   ├── test_p2_offline.py            P2 離線定位與 GIS 輸出
│   ├── test_p2_distribution.py       P2 公開取得／發布
│   └── test_p3_tgos.py               P3 TGOS／回補測試
├── .env.example                      可用環境變數範例
├── .gitignore                        本機資料、憑證與產物排除規則
├── AGENTS.md                         coding agent 執行契約
├── pyproject.toml                    Python、uv、pytest 設定
├── uv.lock                           固定完整 Python 依賴
├── requirements.txt                  相容依賴清單
├── LICENSE                           程式碼授權
└── README.md                         專案入口與使用說明
```

### 資料目錄邊界

| 位置 | 是否進 Git | 內容與使用方式 |
| --- | --- | --- |
| `data/sources/` | 是 | 固定輸入版本、來源清單、檔案雜湊與發布聲明；不放來源資料本體。 |
| `data/reference/` | 是 | 小型路名參考資料及其來源紀錄。 |
| `data/registry/` | 部分 | `garbled_override.csv` 進 Git；執行時產生的 land、parking、no_doorplate、unresolvable CSV 不進 Git。 |
| `data/releases/` | 是 | 小型公開版本指標；大型月檔、年包與維護包實體放在 GitHub Releases。 |
| `data/raw/` | 否 | 使用者自行準備的原始 ZIP。安裝與測試不會下載真實 raw。 |
| `data/work/` | 否 | ingest、normalize、converted、離線索引、地址池與狀態的不可變工作快照。 |
| `data/cache/` | 否 | 已下載並核對的官方來源或其他可重建快取。 |
| `data/output/` | 否 | 待驗證或待發布的月檔、年度 ZIP、manifest 與維護包。 |
| `data/downloads/` | 否 | 從 GitHub Release 取得的公開資料及維護狀態。 |
| `data/tgos/` | 否 | 交給操作員的 TGOS CSV、批次 manifest 及人工交換檔；不得提交。 |

相關專案 `C:/Code/taiwan-address-data` 是本 repo 的同層 checkout，不是本
repo 的子目錄。主專案只在 `data/sources/address_source.json` 記錄它的固定
commit、檔案大小與雜湊。地址 repo 的完整 `roads/` 不複製到本 repo。

### 程式分層

| 層次 | 主要檔案 | 責任 |
| --- | --- | --- |
| 命令入口 | `__main__.py`、`cli.py`、`p2_cli.py` | 解析命令列參數，將 P1～P3 命令交給對應模組。 |
| 共用契約 | `contracts.py`、`p2_contracts.py`、`parquet_io.py` | 驗證 schema、型別、主鍵、筆數及跨表關聯。 |
| 快照核心 | `processing.py`、`snapshots.py`、`sources.py` | 固定輸入／設定雜湊，寫不可變階段結果並保留前版。 |
| P1 轉換 | `ingest.py`、`normalize.py`、`converted.py` | 將 raw ZIP 轉為可追溯的內部 Parquet。 |
| P2 離線定位 | `offline_lookup.py`、`address_pool.py`、`address_state.py` | 建立門牌索引與唯一地址池，分出 located、conflict、unmatched。 |
| P2 輸出發布 | `export.py`、`packaging.py`、`distribution.py` | 產生月檔／年包、驗證維護包並更新 GitHub 公開指標。 |
| P3 TGOS／回補 | `tgos.py`、`backfill.py` | 保留每日配額、人工交換、嚴格匯入及重建受影響月／年。 |
| 舊入口相容 | `0_parse_raw.py`、`1_normalize.py`、`address.py` | 保留既有呼叫方式；新版工作優先使用總 CLI。 |

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
