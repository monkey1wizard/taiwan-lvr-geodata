# P2／P3 觀測、地址狀態、TGOS 與 GIS 契約

P0／P1 的來源觀測契約在 `schemas/`。P2／P3 的內部 Arrow 契約在 `lvr_pipeline/p2_contracts.py`，公開 GeoParquet 契約在 `lvr_pipeline/export.py`。資料目前使用 schema_version 1.0 與 building key v2。P3 以新的 `tgos-state` 子快照擴充 P2 `offline-state`，不改寫既有 P2 schema。

## 粒度與金額

來源觀測以 `raw_record_id` 識別，綁定輸入 SHA-256、ZIP 成員及實際來源列位置。相同序號不能證明相同交易或修訂順序。沒有足夠證據時，`transaction_key=null`、`record_grain=source_observation`。每份觀測保留一次 `amount_minor`，金額尺度與幣別在觀測列保存。地址成員不持有另一份金額。

交易月份 `tx_yyyymm` 經過民國日期驗證，與 raw 批次季度分開。月檔及年度包以同一來源觀測集產生。各類別與格式的 ID、屬性、幾何、筆數及 null 相同。

## 地址與證據關聯

| 表 | 粒度／用途 |
| --- | --- |
| unique_addresses | 每個 key_version／building_key 一列，record_count 是地址成員出現次數 |
| address_occurrences | 每個 component_id 一列，連到 raw_record_id，保存原鍵、採用鍵與原因 |
| address_observations | 每個 evidence_id 一列，保存原來源雜湊／列參照與有效性，不捨棄重複／衝突 |
| address_index | 每個 v2 鍵一列，精確有效座標只有一組時為 located，多組為 conflict |
| unmatched_addresses | address_index 中非 located 的完整子集，包括 conflict 與 outside_scope |
| verified_aliases | 每個有效 alias_key 一列，只有完整門牌及行政區一致的 TGOS 證據可新增 |
| tgos_results | P2 相容空容器，保留原 schema；P3 不將它當成配額帳本 |
| tgos_batches | 每個批次一列，保存服務日期、外部已用配額、保留筆數、狀態與 CSV／回傳雜湊 |
| tgos_queries | 每個送交地址一列，保存批次內順序、查詢指紋、地址鍵與結果狀態 |
| tgos_imports | 每個回傳列一列，保存原回傳雜湊、列號、座標、失敗或拒絕原因 |
| alias_events | 每次別名驗證或撤銷一列，讓目前有效別名可由事件證據核對 |

`source_commit` 表示固定行政區／地址 repo 參考版本。座標本身的來源由 evidence_id 關聯到 `source_ref`、`input_sha256` 與 `source_row_number`，不能將行政區 commit 誤稱為官方座標 CSV 的 Git 版本。

v2 鍵保留號後子門牌及無道路村落，不以 legacy key、路名家族、距離或相同座標合併門牌身分。來源行政區別名須有固定代碼表中的明確同碼證據。亂碼候選必須是唯一候選，且完整門牌有唯一精確座標證據才能套用。

## TGOS 配額與狀態

每個服務日期的上限為 10,000 筆，每個 CSV 也不超過 10,000 筆。`external_used` 是操作員確認的同一帳號、同一服務日期、但不在目前 `tgos-state` 中的已用筆數。程式以 `10,000 - external_used - 目前狀態已保留筆數` 計算可用額度。`prepared`、`submission_unknown`、`submitted` 與 `completed` 都占用配額。只有尚未上傳的 `prepared` 批次取消時才釋放保留。

`prepare-tgos` 先提交包含保留的 `tgos-state`，再交付 UTF-8-sig `addresses.csv` 與 manifest。候選只來自 `unmatched` 或 `outside_scope`，排除 `located`、`conflict` 及所有既有查詢指紋。相同查詢失敗或取消後不會自動原樣重送。人工核准重試時必須指定既有 query fingerprint 與原因，且前次狀態必須是 failed、rejected 或 cancelled。未來服務日期、空批次、過時 `tgos-state` 及無剩餘配額都會失敗。同一服務日期後續提供的 `external_used` 不得小於先前紀錄。

批次狀態只能依 `prepared → submitted／submission_unknown／cancelled`、`submission_unknown → submitted／cancelled` 及 `submitted → cancelled` 移動。`submission_unknown` 不能因逾時自動釋放。多個 agent 必須使用同一份最新 `tgos-state`；舊 snapshot 不能再建立批次。

## TGOS 回傳與回補

`import-tgos` 要求 UTF-8-sig CSV 至少包含 `Address`、`Response_Address`、`Response_X`、`Response_Y`。回傳 `Address` 必須與原批次一對一且集合完全相同。程式不要求服務回傳 id，也不猜測座標軸或 CRS。成功座標必須落在宣告的 WGS84 臺灣範圍內。空座標記為查無結果，單軸缺值、軸疑似對調或超出範圍則記為拒絕。

相同批次及回傳 SHA-256 重複匯入時直接沿用既有 snapshot。不同回傳造成不同有效座標時，地址轉為 `conflict` 並保留全部證據。每次成功匯入重建 `address_index` 與 `unmatched_addresses`，不修改來源觀測粒度或金額。

`backfill-output` 以 P3 子快照重建同一交易月範圍，逐一比較前版及新版月檔 SHA-256。報告列出受影響月份／年份與未變月檔數，前版 output 必須仍可完整驗證。公開發布仍受各座標來源的再散布證據限制；離線命中不會自動取得公開授權。

## 幾何與下載

沒有定位點為 null。一個唯一點為 Point。買賣／預售有多點且 bbox 寬高皆正時為近似 Polygon，同軸退化時為 MultiPoint。租賃多點為 MultiPoint。部分成員定位仍保留幾何並標記 partial。`is_approximation=true` 的 Polygon 不是地籍或建物輪廓。

GeoParquet 使用 1.1.0、CRS84 與 WKB，metadata 的 geometry_types 與實際資料一致。GeoJSON 與 NDJSON 使用相同 Feature。空 NDJSON 只有一個空白換行，零筆記錄。一般讀取器應忽略空行。

月檔保留 `YYYYMM_category` 名稱。年度 ZIP 包含該快照已發布月檔的原始位元組。`scope_limited` 與 `empty_in_scope` 只說明指定輸入範圍；absent 月份不能當作沒有交易。來源、顯名、限制與狀態位於公開 manifest、NOTICE 及維護包。
