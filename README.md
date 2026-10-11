# taiwan-lvr-geodata

將台灣實價登錄的買賣、預售屋與租賃資料整理為可追溯的地理資料。規劃中的成品為 GeoParquet、GeoPackage、CSV 與 GeoJSON，只收錄已有座標的地址。

專案目前進入重建階段。目標是一次完成全部已盤點歷史資料的離線地址處理，再用 TGOS 補充未定位地址。工作順序見[重建企劃](docs/plans/重建企劃.md)，目前流程見[資料處理流程](docs/資料處理流程.md)。

## 目前資料範圍

| 項目 | 狀態與限制 |
| --- | --- |
| 公開資料 | 目前沒有公開版本。舊版 `data-p2-115q1-offline-v2` 因不合格，已於 2026-10-11 刪除 |
| 全歷史處理 | 58 批來源已完成離線處理驗收，成品依新的輸出規格重產中 |
| TGOS 回補 | 已發現回傳門牌不符卻被採用的問題，修復中，既有回補結果須重新驗證 |

TGOS 問題的案例與量測限制見[問題報告](docs/records/TGOS地址錯配.md)。重建不沿用受影響項目的舊驗收結論。

## 下載資料

新版成品完成後，下載位置與方式如下。目前尚未提供。

| 格式 | 位置 | 用途 |
| --- | --- | --- |
| GeoParquet | GitHub release 的 `taiwanlvr_transactions.parquet`、`taiwanlvr_doors.parquet`，各含全部資料 | 程式、資料庫 |
| GeoPackage | `data/output/gpkg/taiwanlvr_YYYY.gpkg`，每年一檔，三類各有交易與門牌兩個圖層 | QGIS、ArcGIS |
| CSV | `data/output/csv/YYYY/`，門牌圖層的整年檔與單月檔 | 試算表、BI、Kepler.gl、AI |
| GeoJSON | `data/output/geojson/YYYY/`，交易與門牌圖層的單月檔 | 網頁地圖 |

`data/output/catalog.json` 列出每個檔案的格式、年、月、類別、筆數與雜湊。完整規格見[重建企劃 6.5](docs/plans/重建企劃.md)。

輸出照實呈現交易。一筆交易登記多個門牌而只有一個總價時，不分攤也不推算。交易圖層一筆交易一個圖徵，含該交易所有門牌的點，在 GIS 點選時一起亮起，總價只出現一次。門牌圖層一個門牌一列，加總金額時只計 `price_counted` 為 1 的列，否則同一筆交易的金額會重複計入。欄位說明見[輸出欄位](docs/輸出欄位.md)。

資料來源、顯名與限制以 `data/output/NOTICE.json` 為準。程式碼授權不代表第三方資料採用相同授權。

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
