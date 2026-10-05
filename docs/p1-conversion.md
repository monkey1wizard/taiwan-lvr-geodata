# P1：逐批轉換與內部快照

P1 已提供 `ingest`、`normalize`、`export-converted` 與 `verify-converted` 命令，對應 T-05～T-07。所有本機修改、驗收與資料產生都在 `C:/Code/taiwan-lvr-geodata` 進行。這是 Parse／Normalize 階段的內部快照，P2 才進行離線定位與使用者月／年 output。

## 執行命令

在 repo 根目錄使用 uv.lock 的固定環境。`--batch` 可重複指定，但必須在來源清單內。命令不下載 raw、不讀憑證，也不呼叫 TGOS。

```powershell
uv run --locked --python 3.13.16 python -m lvr_pipeline export-converted --batch 115q1 --cutoff 202610 --run-id my-115q1-run
```

raw 預設為 `data/raw/`，manifest 預設為 `data/sources/raw_manifest.json`，工作資料預設為 `data/work/`。`--cutoff` 必須明確指定，以交易日期判斷月份，不使用執行當天的時間。重新執行或設定／程式改變時，使用新的 run ID。省略 run ID 時，以程式、設定與輸入的指紋尋找已驗證快照，相同名稱卻有不同綁定會拒絕覆寫。

也可逐階段執行：

```powershell
uv run --locked --python 3.13.16 python -m lvr_pipeline ingest --batch 115q1 --run-id example-ingest
uv run --locked --python 3.13.16 python -m lvr_pipeline normalize --input data/work/ingested/snapshots/example-ingest --cutoff 202610 --run-id example-normalized
uv run --locked --python 3.13.16 python -m lvr_pipeline export-converted --input data/work/normalized/snapshots/example-normalized --cutoff 202610 --run-id example-converted
uv run --locked --python 3.13.16 python -m lvr_pipeline verify-converted --input data/work/converted/snapshots/example-converted
```

對既有 normalized 快照匯出時，cutoff 必須與該快照一致。要改日期截止或補字規則，重新執行 normalize，不能只改匯出參數。

## 記錄與資料契約

| 資料集 | 用途 |
| --- | --- |
| ingest-record | ZIP 成員、起始／結束實體行號、原始欄名與列值、解析狀態。 |
| observation | 每個保留來源記錄一列，金額只存一次，未證明交易身分時 transaction_key 為 null。 |
| address-component | 展開後地址、ordinal、v2 鍵與展開證據狀態，不包含金額。 |
| exclusion | 土地、純車位、無門牌及無效日期的去向。 |
| diagnostic | 欄數異常、金額／面積異常、缺字與地址覆核原因，可與保留或排除記錄重疊。 |
| disposition | 每個來源記錄唯一的 retained／excluded／failed 去向，保留原始欄名與列值供核對。 |

Typed Parquet 欄位及 metadata 見 [Arrow 契約](../schemas/converted-parquet.json)。P0 的四種 JSON Schema 繼續驗證列內容，跨分割區的唯一鍵、外部索引鍵及去向由 DuckDB 檢查。原始欄位以 JSON 文字存於 Parquet，讀取驗證時還原為物件。

`raw_record_id` 使用 input SHA-256、member path 與起始實體行號。跨行 CSV 的結束行號另存。英文說明列需同時符合已知地址及交易標的說明文字，且日期欄不是數字，不能只依地址文字或第二列位置跳過。重複標頭與空白行另計數。欄數異常列保留原值並標為 failed，不填補欄位或默默丟棄。

來源欄位明確對應版本規則：買賣使用「交易年月日／總價元」，預售屋支援「交易年月日／總價元」及既有欄名，租賃使用「租賃年月日／總額元／建物總面積平方公尺」及既有欄名。同義欄位若同時有不同值，保留診斷，不任選一欄。

金額以整數分保存，currency 為 TWD、amount_scale 為 100。超出整數範圍或不能精確表示為分的值不四捨五入，原值仍保留。面積保存明確的平方公尺十進位字串。零與缺值不同。所有原始欄位，包括未對應欄位，保存在 props_json 或 disposition 內。

只展開可證明的明列門牌清單，保留全部子門牌。連字號區間、缺字、行政範圍不明或展開歧義進覆核，地址鍵可為 null。既有補字表按固定雜湊套用，離線座標探查屬 P2，不在 P1 自動猜字。

## 提交與中斷

每一階段先寫 build，再建立不可變 snapshots/<ID> 與 manifest，最後切換 current.json。每個附件包含 SHA-256、大小、格式、筆數及 schema。producer_config 保存參數與程式／契約檔案指紋，並標示來源程式是否尚未提交。指紋正規化文字換行，使 Windows／Linux checkout 可核對相同程式內容。

發布前與重新讀取時都檢查結構、筆數、關聯、來源批次／雜湊與 cutoff。舊快照保留，過時 parent 或中斷不得覆蓋已完成指標。未完成階段不自動當成快取完成，使用新 run ID 重跑。不得自行刪除殘留發布鎖。

Arrow 同時限制列數與每個 writer 的 8 MiB 字串／數值負載。超過單列預算時明確失敗，不能截斷資料。讀取保留 row-group 邊界。DuckDB 驗證使用 2 執行緒、256 MB 記憶體上限及 1 GiB 暫存上限。Python／Arrow 另有記憶體需求，因此仍量測整個行程的 RSS。

寫入前用 manifest 的解壓縮大小估算工作磁碟，保留 20% 餘裕。這是保守估算，不是實測壓縮比。全歷史與其他 runner 的效能仍屬 P5 驗收，不能以單季結果保證。

## 驗收紀錄

日期：2026-10-05，Asia/Taipei。Windows 固定 Python 3.13.16 環境已通過 178 個測試，包含既有 145 個及新增 33 個 P1 測試。涵蓋 TP-02～TP-07 與 TP-10 的 P1 範圍。GitHub Ubuntu 的 [PR CI](https://github.com/monkey1wizard/taiwan-lvr-geodata/actions/runs/37268397622) 已通過 178 個測試，24.08 秒，Python 3.13.16。測試程式版本為 `6f70b4e7355155a82a1d7df775f9400af1fdff18`。

記憶體成長測試使用獨立行程處理 1,000／120,000 列合成輸入，Arrow writer 維持 256 列上限。測試要求大輸入行程峰值低於 512 MiB，且相對小輸入增加不超過 96 MiB。完整量測與真實批次結果見 [P1 證據](p1-evidence.json)。測試及安裝不讀取真實 raw。

真實 `115q1` 已使用提交 `f79e0750fd0a0a0abb127f21ad835552cd7c8c6a` 處理，來源程式狀態為 clean。後續匯出 cutoff 防誤用與說明列防誤判修正另由測試驗證。115q1 的 64 個說明列已逐一核對，修正不改變此批次的計數。驗收命令為：

```powershell
.venv/Scripts/python.exe -m lvr_pipeline export-converted --batch 115q1 --cutoff 202610 --batch-rows 1024 --run-id p1-115q1-accepted
```

| 項目 | 筆數／結果 |
| --- | --- |
| 來源記錄 | 129,058 |
| 保留 observations | 102,743 |
| 排除 exclusions | 26,314 |
| 失敗來源列 | 1 |
| address_components | 102,881 |
| diagnostics | 9,720 |
| dispositions | 129,058 |
| 轉換 Parquet | 54,430,623 bytes，約 51.91 MiB |
| 完整處理時間 | 192.35 秒 |
| 行程 RSS 峰值 | 423,309,312 bytes，約 403.7 MiB |

排除項為土地 17,419、純車位 1,047、無門牌 7,842、無效日期 6。欄數異常出現在 `a_lvr_land_b.csv` 的第 883 個實體行，原欄名及列值保留在 disposition／diagnostic，不宣稱該列解析成功。

保留＋排除＋失敗等於全部來源記錄。diagnostics 可以與其他資料重疊，不加入交易筆數。amount_minor_sum 僅供內部核對，不是跨類別的市場統計。

已完成快照位於 `data/work/converted/snapshots/p1-115q1-accepted`。manifest SHA-256 為 `5308ced7c501584a6aaedfab8c72193f909471fdc82dfd7700b65390dfe111ba`。產物只在本機，尚未持久交接或公開發布，也未定位地址。下一階段可依 T-10／T-11 等前置條件建立離線索引與全域地址池，不自動啟動 P2。

## API 依據

逐批 Parquet 讀取依 [Apache Arrow 官方 API](https://arrow.apache.org/docs/python/generated/pyarrow.parquet.ParquetFile.html)。跨檔案查詢與暫存 view 依 [DuckDB 官方 relational API](https://duckdb.org/docs/current/clients/python/relational_api)。套件版本以 repo 的 uv.lock 為準，驗收結果以本文件及 CI 紀錄為準。
