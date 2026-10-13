# 名詞與程式名稱

本文件只統一常用詞與程式碼名稱。資料關聯見[資料契約](資料契約.md)，地址術語見[地址規則](地址規則.md)，公開欄位見[輸出欄位](輸出欄位.md)。

## 資料單位

| 用詞 | 程式或契約名稱 | 說明 |
| --- | --- | --- |
| 交易 | `transaction_id` | 一筆來源主檔的一列，不跨批次合併。公開 id 為 `{src_batch}/{member_path}/{source_row_number}`，批次雜湊記在 `catalog.json` |
| 來源觀測 | `source_observation`、`raw_record_id` | 原始交易主檔的一列及其來源識別 |
| 地址成員 | `address_component`、`component_id` | 從觀測的地址欄拆出的一個門牌成員；成員序號從 0 起算 |
| 地址鍵／門牌鍵 | 唯一目標名稱 `building_key` | 使用重新驗證後的現行 v2 地址規則，規則版本記於 metadata。2026-10-12 名稱遷移完成，舊名稱已自程式移除，不保留相容別名或舊結果 fallback |
| 定位證據 | `evidence_id` | 地址或座標判定所依據的一筆來源證據 |
| 快照 | `snapshot` | 經驗證、綁定特定輸入與規則的階段資料 |
| Run | `run`、`run_id` | 一次固定版本的資料處理執行 |

## 狀態及處理結果

| 用詞 | 程式碼 | 說明 |
| --- | --- | --- |
| 已定位 | `located` | 存在可採用座標的地址鍵 |
| 未命中 | `unmatched` | 固定地址來源沒有符合的地址候選 |
| 衝突 | `conflict` | 候選地址或座標有歧義，未採用定位 |
| 範圍外 | `outside_scope` | 來源或查詢不在本次設定的處理範圍內。暫停縣市使用 `conflict` 與 `address_source_suspended`，不使用此狀態 |
| 待查核表 | `exception_addresses` | 有問題或未定位原因的觀測地址清單 |
| 人工交叉比對清單 | `cross_check_addresses` | 候選涉及多門牌歧義，需人工比對的清單 |
| 第二類人工查核區 | — | TGOS 後仍未定位的獨立查核集合，新版尚未實作 |

## 來源及日期

| 用詞 | 程式或欄位名稱 | 說明 |
| --- | --- | --- |
| 批次 | `src_batch` | 原始季度來源識別，例如 `115q1`。內部欄位，公開輸出中是 `transaction_id` 的第一段 |
| 類別 | `category` | `sales`（買賣）、`presale`（預售屋）或 `rent`（租賃） |
| 交易日期 | `trade_date` | 單筆來源觀測記載的日期 |
| 交易年月 | `tx_yyyymm` | 由合法交易日期換算的年月 |
| 已知空批次 | — | 來源證據確認沒有資料的批次。缺少檔案不等於已知空批次 |
| TGOS | `prepare-tgos`、`import-tgos` | 以人工交換檔進行的門牌比對服務流程 |

## 輸出名稱

| 用詞 | 欄位或檔案名稱 | 說明 |
| --- | --- | --- |
| 多門牌交易 | `door_count > 1` | 一個來源觀測拆出多個門牌。沒有獨立欄位 |
| 門牌序號 | `door_index` | 新版輸出欄位，從 1 起算。與 `transaction_id` 合為公開主鍵 |
| 門牌文字 | `address` | 這個點代表的單一門牌，是地址欄拆解後的文字 |
| 交易地址原文 | `transaction_addresses` | 來源觀測的原始地址欄文字 |
| 交易識別碼 | `transaction_id` | 新版輸出欄位，可讀自然鍵，見資料單位表 |
| 檔案目錄 | `catalog.json` | 新版公開資料的檔案、範圍、筆數、大小及雜湊索引 |
| 來源顯名 | README 公開資料節 | 資料來源與授權的固定文字。2026-10-13 起不設 NOTICE 檔；每版變更寫在 GitHub release 說明 |

程式中仍存在的舊名稱只供描述現況或定位歷史證據，不代表新流程允許相容讀取或沿用舊結果。門牌鍵唯一名稱是 `building_key`；`building_key_v2` 與 `legacy_building_key` 已於 R07-6b 移除。
