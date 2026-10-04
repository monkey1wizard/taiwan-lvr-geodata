# 資料來源與本機輸入

本 repo 只包含路名參考資料與人工補字規則。原始實價登錄 ZIP、第三方門牌座標庫、舊定位成果與工作資料沒有搬入。

## 實價登錄原始 ZIP

來源為內政部[不動產交易實價查詢服務網的開放資料下載頁](https://plvr.land.moi.gov.tw/DownloadOpenData)。

既有解析程式讀取 `data/raw/*lvr_landcsv.zip`，例如 `data/raw/115q1_lvr_landcsv.zip`。下載 CSV 格式的全國 ZIP 後，保持壓縮格式，放入本機 `data/raw/`。

原始 ZIP 不納入 Git。來源專案的原始 ZIP 仍保留在 `C:\Code\taiwan-lvr-geojson\data\raw\`，本次沒有複製。

既有來源文件記錄實價登錄資料使用政府資料開放授權條款第 1 版。後續發布重建資料時，需核對來源當時的授權與顯名要求。

## 已搬入的路名資料

- 檔案：`data/reference/roadnames_35321_20260618.csv`。
- 來源紀錄：[`roadnames_35321.provenance.txt`](../data/reference/roadnames_35321.provenance.txt)。
- 該紀錄標示資料集 ID 為 `35321`，下載日期為 2026-06-18。
- 本次保留來源檔案與來源紀錄，沒有重新下載或更新資料。

## 人工補字規則

`data/registry/garbled_override.csv` 來自舊專案。它保存缺字與異體字的既有補正规則及證據欄位。

若需使用其他規則檔，請設定 `GARBLED_PATH`。程式從執行環境讀取這個變數，不會自動讀取 `.env`。

## 離線門牌座標來源與 TGOS

新版草稿預計使用 `taiwan-address-data` 與 TGOS。這次只搬入可沿用的地址處理程式，尚未實作新版離線定位與 TGOS 流程。

第三方門牌座標資料、TGOS 憑證與回傳結果沒有搬入。舊版來源與人工操作紀錄見 [legacy 索引](legacy/README.md)，只供重寫參考。
