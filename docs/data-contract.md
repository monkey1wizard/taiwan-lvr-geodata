# P2 觀測、地址狀態與 GIS 契約

P0／P1 的來源觀測契約在 `schemas/`。P2 的內部 Arrow 契約在 `lvr_pipeline/p2_contracts.py`，公開 GeoParquet 契約在 `lvr_pipeline/export.py`。資料目前使用 schema_version 1.0 與 building key v2。這份文件只說明已實作的離線流程，TGOS 保留／配額及別名回補在 P3。

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
| verified_aliases | P2 初版為空，已驗證別名與撤銷由 P3 實作 |
| tgos_results | P2 初始空容器，tgos_started=false，不代表已具備 P3 的配額操作 |

`source_commit` 表示固定行政區／地址 repo 參考版本。座標本身的來源由 evidence_id 關聯到 `source_ref`、`input_sha256` 與 `source_row_number`，不能將行政區 commit 誤稱為官方座標 CSV 的 Git 版本。

v2 鍵保留號後子門牌及無道路村落，不以 legacy key、路名家族、距離或相同座標合併門牌身分。來源行政區別名須有固定代碼表中的明確同碼證據。亂碼候選必須是唯一候選，且完整門牌有唯一精確座標證據才能套用。

## 幾何與下載

沒有定位點為 null。一個唯一點為 Point。買賣／預售有多點且 bbox 寬高皆正時為近似 Polygon，同軸退化時為 MultiPoint。租賃多點為 MultiPoint。部分成員定位仍保留幾何並標記 partial。`is_approximation=true` 的 Polygon 不是地籍或建物輪廓。

GeoParquet 使用 1.1.0、CRS84 與 WKB，metadata 的 geometry_types 與實際資料一致。GeoJSON 與 NDJSON 使用相同 Feature。空 NDJSON 只有一個空白換行，零筆記錄。一般讀取器應忽略空行。

月檔保留 `YYYYMM_category` 名稱。年度 ZIP 包含該快照已發布月檔的原始位元組。`scope_limited` 與 `empty_in_scope` 只說明指定輸入範圍；absent 月份不能當作沒有交易。來源、顯名、限制與狀態位於公開 manifest、NOTICE 及維護包。
