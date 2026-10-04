# taiwan-lvr-geodata

本專案保留台灣實價登錄資料的解析與地址處理程式，作為新版地理資料管線的起點。

新版企劃仍是[草稿](docs/drafts/taiwan-lvr-geodata-新版資料處理流程.md)。Parquet／DuckDB 工作流程、全歷史地址池、TGOS 結果匯入與 GIS 輸出尚未實作。

## 目前內容

- `lvr_pipeline/0_parse_raw.py`：讀取原始 ZIP，分流買賣、預售屋與租賃，另存土地與純車位。
- `lvr_pipeline/1_normalize.py`：套用人工補字規則，產生乾淨地址與待補字地址清單。
- `lvr_pipeline/address.py`：地址解析、正規化與 `building_key()`。
- `lvr_pipeline/garbled.py`、`garbled_resolve.py`：人工缺字補正與路名候選處理。
- `lvr_pipeline/tx_date.py`：民國日期轉交易年月。
- `tests/`：上述功能的六個既有測試檔。
- `data/registry/garbled_override.csv`：既有人工補字規則。
- `data/reference/`：路名參考資料與來源紀錄。

這些程式沿用舊版 CSV 工作格式。保留程式碼不代表它們已符合新版企劃。

## 執行測試

目前保留的執行程式只使用 Python 標準函式庫。測試使用 pytest。

在專案根目錄執行：

```powershell
python -m pip install -r requirements.txt
python -m pytest -q
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

- [新版資料處理流程草稿](docs/drafts/taiwan-lvr-geodata-新版資料處理流程.md)：Google Drive 文件的完整內容快照，後續將重寫。
- [舊文件參考索引](docs/legacy/README.md)：保留可再利用的舊文件原文，列出已知過時內容。
- [資料來源](docs/DATA_SOURCES.md)：本次搬移所需的來源與本機資料放置方式。

舊 SQLite 程式、定位成果、一次性回灌工具、原始 ZIP 與工作資料沒有搬入本 repo。原專案仍可作為後續改寫參考。

## 來源與授權

程式碼與既有測試複製自 `taiwan-lvr-geojson`。來源 Git HEAD 為 `5be26dc3e8b3a6425abb9b56f8fe4b9d9f8e187c`，搬移日期為 2026-10-04。

程式碼授權見 [LICENSE](LICENSE)。路名資料的來源與授權紀錄見 [provenance](data/reference/roadnames_35321.provenance.txt)。第三方門牌座標資料未包含在本 repo。
