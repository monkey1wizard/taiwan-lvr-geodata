# TGOS 批次匯入紀錄

本次匯入批次 `tgos-0358ee6e90df` 的 TGOS 回傳。這是快速版，非 V-12 驗收，結果不作為 V-12 通過依據。

## 範圍與版本

| 項目 | 填寫內容 |
| --- | --- |
| 日期、執行者 | 2026-10-07，Claude，Asia/Taipei |
| 工作 R 編號、驗收 V 編號 | 任務卡 F-1（快速資料路徑，重建企劃 8.1、8.2）。快速版，非 V-12 驗收 |
| 程式提交、規則與契約版本 | 提交 `0804698`。地址鍵 `building_key_v2`。匯入只採用回傳地址與送出地址同址的結果，其餘隔離，不建立別名 |
| 環境、工具版本 | Windows 11，專案 `.venv` Python |
| 輸入位置、固定版本、SHA-256 | `data/tgos/20261007-0358ee6e90df/Address_Finish.csv`，1,104,401 bytes，10,000 筆資料列，SHA-256 `31839e1a909d4bbd68927136a3e334f885547afcce0532600700f83f5cebb288`。送出檔 `addresses.csv` SHA-256 `a6b3cc96d73ab95ddbf1d88ec2ac2c45ee86ee38d89ddcc805508e2ea832f734` |
| 選定批次、類別、月份、截止日期 | 批次 `tgos-0358ee6e90df`，10,000 筆。擁有者於 2026-10-07 確認已上傳並取得回傳 |
| 未包含範圍 | 回補輸出、發布、地址 patch、下一批交換檔、V-12 完整驗收 |

## 命令與結果

| 命令與工作目錄 | 預期 | 實際 | 結束代碼 | pass／fail／not-run | 證據位置與雜湊 |
| --- | --- | --- | --- | --- | --- |
| 計算回傳檔雜湊、大小、列數（專案根目錄） | 10,000 筆資料列，欄位 `id,Address,Response_Address,Response_X,Response_Y` | 1,104,401 bytes，10,000 筆，欄位相符 | 0 | pass | SHA-256 `31839e1a…cebb288` |
| 以 UTF-8-sig 讀兩份 CSV，比對 `(id, Address)` 集合 | 回傳與 `addresses.csv` 完全一致 | 兩邊各 10,000 筆，集合完全相同 | 0 | pass | 本紀錄執行階段輸出 |
| `python -m lvr_pipeline set-tgos-status --state data/work/tgos-state/snapshots/tgos-state-ee379a73762041dbf40978b6 --batch tgos-0358ee6e90df --status submitted --reason "owner confirmed upload; response received 2026-10-07"` | 狀態由 `prepared` 轉為 `submitted`，產生新快照 | 完成，未拒絕轉換 | 0 | pass | 新快照 `tgos-state-db34e07ce50206960eb8ba9e`，manifest SHA-256 `dc8845c7f5c6a2222dc53b6a235815dd0569a7e2b1d494522ec3121ce393f49c` |
| `python -m lvr_pipeline import-tgos --state data/work/tgos-state/snapshots/tgos-state-db34e07ce50206960eb8ba9e --batch tgos-0358ee6e90df --response data/tgos/20261007-0358ee6e90df/Address_Finish.csv` | 匯入完成並產生新快照 | `completed: true`，耗時 15 分 53.8 秒 | 0 | pass | 新快照 `tgos-state-6800e64bc3279d7acb0f2f46`，manifest SHA-256 `a215e299bcd40d14d2ac51c3d5cc2575d92b295d94b8775c80e118da18bfea69` |
| `python -m lvr_pipeline verify-tgos-state --input data/work/tgos-state/snapshots/tgos-state-6800e64bc3279d7acb0f2f46` | `verified: true` | `verified: true`，批次數 1，狀態計數 conflict 10,143、located 918,523、outside_scope 0、unmatched 176,763 | 0 | pass | 新 TGOS 快照 |
| 以 pyarrow 讀 `tgos_batches`、`tgos_imports`、`alias_events`、`verified_aliases` 並統計 | 四類數量相加為 10,000，無別名 | 見下節 | 0 | pass | 新 TGOS 快照 Parquet |

## 資料核對與資源

批次 `tgos-0358ee6e90df` 的 `tgos_batches` 狀態為 `completed`，`response_sha256` 與回傳檔雜湊一致，`reason` 為擁有者確認上傳的說明。`tgos_imports` 共 10,000 列，與送出筆數相同。

| 類別 | 筆數 | 對應 `tgos_imports` 內容 |
| --- | --- | --- |
| 同址採用 | 3,188 | `status = succeeded` |
| 地址不符隔離 | 4,037 | `status = rejected`，原因 `TGOS response address differs from submitted address` |
| 查無門牌 | 2,739 | `status = failed`，回傳地址為 `找不到指定的門牌地址。` |
| 座標無效 | 11 | `status = rejected`，原因 `TGOS coordinate is outside declared WGS84 Taiwan bounds` |
| 回傳地址不完整（程式另列的第五類） | 25 | `status = rejected`，原因 `TGOS response is not a complete address` |
| 合計 | 10,000 | 3,188 + 4,037 + 2,739 + 11 + 25 = 10,000 |

任務卡的四類加上程式另列的「回傳地址不完整」，合計 10,000。若把不完整地址併入隔離，隔離合計為 4,073（`rejected` 全部）。有座標的列為 3,188 列，與採用數相同。

別名檢查：`alias_events` 0 列、`verified_aliases` 0 列，沒有建立別名。`tgos_results` 也是 0 列，本次採用結果只記在 `tgos_imports`，不代表匯入失敗。

## 結論與限制

快速版匯入完成，新快照 `tgos-state-6800e64bc3279d7acb0f2f46` 通過 `verify-tgos-state`。四類加不完整地址合計 10,000，無別名產生。這份紀錄不是 V-12 驗收，不能作為通過依據。

採用的 3,188 筆只是 TGOS 狀態中的匯入結果，尚未回補任何輸出，也未發布或匯出地址 patch。送出狀態與狀態快照都在忽略 Git 的 `data/` 內。下一步依擁有者指示執行 F-2（下一批交換檔），不在本次範圍。
