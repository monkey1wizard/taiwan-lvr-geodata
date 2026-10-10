# taiwan-lvr-geodata

將台灣實價登錄的買賣、預售屋與租賃資料整理為可追溯的地理資料，提供依交易月份下載的 GeoParquet、GeoJSON、NDJSON，以及年度 ZIP。

專案目前進入重建規劃。目標是一次完成全部已盤點歷史資料的離線地址處理，再用 TGOS 補充未定位地址。文件已依[重建企劃](docs/plans/重建企劃.md)收斂為 README、architecture 與 CONTRIBUTING，程式及資料目錄尚未重構。

## 目前資料範圍

| 項目 | 狀態與限制 |
| --- | --- |
| 公開資料 | 本機[版本指標](releases/latest.json)指向 `p2-115q1-offline-v2`，來源範圍只有 `115q1` |
| 離線定位 | 該公開版本的座標來源限臺北市官方資料，其餘未定位觀測仍保留 |
| 全歷史處理 | 現有清單列出 58 批原始來源，尚未完成全歷史離線處理驗收 |
| TGOS 回補 | 已發現回傳門牌不符卻被採用的問題，既有回補候選與地址補充資料須重新驗證 |

**單批來源中出現多個交易月份，不代表已處理那些月份的全部交易。** 公開版本標示 `scope_limited`，使用時須保留這項限制。

TGOS 問題的案例與量測限制見[問題報告](docs/records/TGOS地址錯配.md)。重建不沿用受影響項目的舊驗收結論。

## 下載資料

本機[版本指標](releases/latest.json)目前指向 `p2-115q1-offline-v2`。此版本只處理 `115q1` 來源，座標來源限臺北市官方資料。本次文件整理未重新連線核對公開附件。

### 選擇資料

從[該版本 Release](https://github.com/monkey1wizard/taiwan-lvr-georeleases/tag/data-p2-115q1-offline-v2)取得 manifest，再依附件清單選擇資料。

| 選項 | 意義 |
| --- | --- |
| `sales`、`presale`、`rent` | 買賣、預售屋、租賃 |
| `YYYYMM_category` 月檔 | 月份來自交易或租賃日期，不是來源 ZIP 季度 |
| GeoParquet | 含地理中繼資料的 Parquet |
| GeoJSON | FeatureCollection |
| NDJSON | 每行一個 Feature |
| 年度 ZIP | 同一版本月檔，依格式分開打包 |

年度包的月份及分片以 manifest 為準。若有多個分片，取得清單所列的全部分片並逐一驗證，不能只下載第一個附件。

### 使用下載命令

先從固定版本指標取得 manifest URL 與 SHA-256。以下大寫參數為占位文字，執行前必須替換。目的目錄應尚未存在。

```bash
uv run --locked --python 3.14.8 python -m lvr_pipeline fetch-output --manifest-url MANIFEST_URL --manifest-sha256 MANIFEST_SHA256 --target data/downloads/my-month --month 202601 --category sales --format geoparquet
```

省略 `--format` 可下載該選擇範圍的三種格式。月份必須在該版 manifest 中。下載後依 manifest 核對檔案大小與雜湊，不把下載成功當成涵蓋完整。

### 解讀涵蓋與定位

`scope_limited` 表示來源範圍受限。單季來源中出現多個交易月份，不代表那些月份的全部交易都已處理。manifest 中未列出的月份與已確認範圍內的空月份不同，不能都解讀為零筆交易。

未定位觀測仍保留，geometry 為 null。部分門牌定位及近似幾何須保留其品質標記。多門牌的 Polygon 不是建物或地籍輪廓。欄位及幾何語意見[資料規格](docs/architecture.md#資料規格)。

資料仍以來源觀測為單位。未證明相同的交易不能視為已跨批次去重。每版來源、顯名及限制以該版 `NOTICE.json` 為準，程式碼授權不代表第三方資料採用相同授權。

### 維護者下載

維護包用於無原始 ZIP 的重產與後續處理。下載參數 `--maintenance`、交接內容與驗證方式見[發布與復原](CONTRIBUTING.md#發布與復原)。一般使用者取得月檔不需要維護包。

## 執行與開發

安裝、來源準備及現有命令見[資料架構與處理](docs/architecture.md#使用與資料處理)。開發、測試與發布方式見[CONTRIBUTING](CONTRIBUTING.md)。全歷史主入口尚待實作，單批命令不代表全量完成。

## 目前目錄

以下是現有布局。重建後的完整目錄樹見[重建企劃](docs/plans/重建企劃.md)。

```text
taiwan-lvr-geodata/
├── README.md          唯一專案入口與下載說明
├── CONTRIBUTING.md    開發、驗證、發布與復原
├── lvr_pipeline/      資料處理程式與命令（contracts/json/ 為現行 JSON Schema）
├── config/            設定範例
├── scripts/           安裝、樣本與驗證工具
├── tests/             合成資料測試
├── docs/              操作文件、規格、計畫與歷史證據
│   ├── architecture.md  來源、處理流程與資料契約
│   ├── task-result-template.md
│   ├── plans/         重建企劃
│   ├── records/       問題、遷移與驗收證據
│   └── archive/       已取代文件
├── data/
│   ├── sources/       納入 Git 的來源描述
│   ├── reference/     小型路名參考與來源紀錄
│   ├── registry/      補字規則及本機分類結果
│   ├── releases/      納入 Git 的公開版本指標
│   ├── raw/           本機原始 ZIP
│   ├── cache/         本機快取
│   ├── work/          本機工作快照
│   ├── tgos/          本機人工交換檔案
│   └── output/        本機發布候選
└── .github/workflows/ Linux 測試與公開成果驗證
```

原始 ZIP、工作快照、TGOS 交換檔案、發布候選與憑證不進 Git。相關地址專案 `C:/Code/taiwan-address-data` 是同層專案，不複製整份地址資料進本專案。

## 文件入口

| 要找的內容 | 文件 |
| --- | --- |
| 月檔、年度包及涵蓋限制 | [本頁下載說明](#下載資料) |
| 從原始資料到離線交付、TGOS 回補的完整流程 | [完整資料處理流程](docs/architecture.md#完整資料處理流程) |
| 來源、操作、資料規格、地址規則與 TGOS | [資料架構與處理](docs/architecture.md) |
| 開發、驗證、發布與復原 | [CONTRIBUTING](CONTRIBUTING.md) |
| 目標目錄、流程與工作順序 | [重建企劃](docs/plans/重建企劃.md) |

專案只有根目錄一份 README。問題與驗收放在 docs/records，已取代文件放在 docs/archive。搬移對照與尚未完成的盤點見[舊成果遷移](docs/records/舊成果遷移.md)。

## 已知限制

TGOS 同址判定尚未修復，既有候選須重新驗證。全歷史離線處理、舊補字案例逐項承接及公開全量交付均未完成。正式文件已標示待實作內容，歷史文件的完成勾選不作為新版驗收。

## 來源與授權

程式與既有測試源自 `taiwan-lvr-geojson`。程式碼授權見 [LICENSE](LICENSE)，路名參考資料見[來源紀錄](config/reference/roadnames_35321.provenance.txt)。

每版資料的來源、顯名與使用限制以該版本的 `NOTICE.json` 為準。程式碼授權不代表第三方地址資料具有相同授權。
