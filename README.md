# taiwan-lvr-geodata

將台灣實價登錄的買賣、預售屋與租賃資料整理為可追溯的地理資料，提供依交易月份下載的 GeoParquet、GeoJSON、NDJSON，以及年度 ZIP。

專案目前進入重建規劃。目標是一次完成全部已盤點歷史資料的離線地址處理，再用 TGOS 補充未定位地址。本輪只更新 README 與[重建企劃](docs/plans/重建企劃.md)，程式及資料目錄尚未重構。

## 目前資料範圍

| 項目 | 狀態與限制 |
| --- | --- |
| 公開資料 | 本機[版本指標](data/releases/latest.json)指向 `p2-115q1-offline-v2`，來源範圍只有 `115q1` |
| 離線定位 | 該公開版本的座標來源限臺北市官方資料，其餘未定位觀測仍保留 |
| 全歷史處理 | 現有清單列出 58 批原始來源，尚未完成全歷史離線處理驗收 |
| TGOS 回補 | 已發現回傳門牌不符卻被採用的問題，既有回補候選與地址補充資料須重新驗證 |

**單批來源中出現多個交易月份，不代表已處理那些月份的全部交易。** 公開版本標示 `scope_limited`，使用時須保留這項限制。

TGOS 問題的案例與量測限制見[問題報告](docs/drafts/問題報告-TGOS匯入與地址重複.md)。重建不沿用受影響項目的舊驗收結論。

## 下載資料

從[目前版本的 GitHub Release](https://github.com/monkey1wizard/taiwan-lvr-geodata/releases/tag/data-p2-115q1-offline-v2)選擇月份、類別與格式。逐檔下載與雜湊核對方式見[下載說明](docs/downloads.md)。

- 類別：`sales` 買賣、`presale` 預售屋、`rent` 租賃。
- 月份：依交易或租賃日期的 `tx_yyyymm`，不是來源 ZIP 的季度。
- 月檔：使用 `YYYYMM_category` 檔名，可單獨下載，不需要取得原始 ZIP。
- 年度包：依格式打包同一版本的月檔，實際月份涵蓋以 manifest 為準。

未定位資料的 geometry 為 null。多門牌產生的近似 Polygon 不是建物或地籍輪廓。資料目前以來源觀測為單位，尚未證明交易身分的記錄不能視為已完成跨批次去重。

## 執行與開發

在專案根目錄執行。套件版本由 `uv.lock` 固定，目前使用 `uv 0.12.23` 與 `Python 3.13.16`。

Linux 安裝與合成測試：

```bash
bash scripts/setup.sh
```

執行前須已安裝 uv。安裝與測試不下載真實交易資料，也不需要 TGOS 憑證。

Windows 可使用相同固定環境：

```powershell
uv sync --locked --group dev --python 3.13.16
uv run --locked --python 3.13.16 python scripts/build_fixtures.py
uv run --locked --python 3.13.16 python -m pytest -q
```

檢視目前已實作的命令：

```bash
uv run --locked --python 3.13.16 python -m lvr_pipeline --help
```

現有轉換命令與資料準備方式見[逐批轉換說明](docs/p1-conversion.md)及[資料來源](docs/DATA_SOURCES.md)。這些是現有操作，不能視為新版全歷史流程已完成。全歷史主入口、續跑與驗收方式將依重建企劃實作後再更新。

正式開發目錄為 `C:/Code/taiwan-lvr-geodata`。在 `main` 修改、測試及提交，再直接推送 `origin/main`。GitHub Actions 負責 Linux 驗證。

## 目前目錄

以下是現有布局。重建後的完整目錄樹見[重建企劃](docs/plans/重建企劃.md)。

```text
taiwan-lvr-geodata/
├── README.md          唯一專案入口
├── lvr_pipeline/      資料處理程式與命令
├── config/            設定範例
├── schemas/           現行資料契約
├── scripts/           安裝、樣本與驗證工具
├── tests/             合成資料測試
├── docs/              操作文件、規格、計畫與歷史證據
│   └── plans/
│       └── 重建企劃.md
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
| 接下來重做什麼、目標目錄與驗收 | [重建企劃](docs/plans/重建企劃.md) |
| TGOS 地址錯配與重複的已知問題 | [問題報告](docs/drafts/問題報告-TGOS匯入與地址重複.md) |
| 月檔及年度包下載 | [下載說明](docs/downloads.md) |
| 目前的輸入準備與轉換命令 | [資料來源](docs/DATA_SOURCES.md)、[逐批轉換](docs/p1-conversion.md) |
| 目前資料欄位、狀態與幾何 | [資料契約](docs/data-contract.md) |
| 開發規則與結果紀錄 | [執行契約](AGENTS.md)、[結果範本](docs/task-result-template.md) |

目前文件仍含舊階段紀錄與已失效敘述，整理方式已列入重建企劃。涉及 TGOS 同址判定及全量完成狀態時，須同時核對問題報告，不能單憑舊文件的完成勾選採用結果。

## 來源與授權

程式與既有測試源自 `taiwan-lvr-geojson`。程式碼授權見 [LICENSE](LICENSE)，路名參考資料見[來源紀錄](data/reference/roadnames_35321.provenance.txt)。

每版資料的來源、顯名與使用限制以該版本的 `NOTICE.json` 為準。程式碼授權不代表第三方地址資料具有相同授權。
