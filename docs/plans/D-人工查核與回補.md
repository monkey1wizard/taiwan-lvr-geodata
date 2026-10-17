# D 人工查核與回補

類別 iii：不論 TGOS 情況都能持續做。修訂日期：2026-10-18。現況與擁有者決定以 [00 主企劃](00-主企劃.md)與[擁有者決定與驗收總表](擁有者決定與驗收總表.md)為準。第二類人工查核區的資料契約見[資料契約](../資料契約.md)「第二類人工查核區」。

## 目的

兩件事。一是把 TGOS 也查不到的門牌整理成一個集合，讓擁有者逐筆判定，判定結果能重複套用。二是把 TGOS 採用的座標與擁有者判定的座標補進已公開的檔案，只重建受影響的月份。這兩個機制與C 段 的每日交換同時建，交換期間每天到的回傳就是測試案例。

## 前置

契約已定。不等 V-11 與 V-12。接 `run-full` 的部分要等B 段 b22 提交後再接，之前只在新模組與新測試檔開工。

## 工作

### D-1 第二類人工查核區（R10-1a）

- [x] d01 在 `build_review` 內建立 `second_review.parquet`、`second_review_observations.parquet` 與同名 CSV，規格依資料契約
- [ ] d02 `run-full` 新增 `--review-decisions <目錄>`，預設 `data/review-decisions/`。目錄下全部判定檔的檔名與 SHA-256 寫入 review 階段綁定，新增判定檔會使 review 與之後階段重建。10-18 已完成 `build_review(review_decisions=...)` 與綁定；`run-full` 旗標等B 段 b12 到 b13 提交後接
- [x] d03 新增進入原因代碼 `tgos_not_found`、`tgos_key_text_not_round_trip`、`district_no_candidate_town`，加入 `EXCEPTION_REASON_CODES`。文字算不回鍵與缺區無候選區要在 review 建置時以同一函式重算，不讀 `prepare-tgos` 的報告
- [x] d04 回傳門牌鍵：帳本中 `response_building_key` 為空的列，建置時以現行 `building_key` 從 `response_address` 重算，算不出時欄位為空並保留原因
- [x] d05 判定檔核對依契約：檔名雜湊不符拒絕、同檔重複查核鍵拒絕、`orphaned` 與 `stale` 不套用並計數。`accept_tgos_response` 核對 `tgos_result_id` 屬於該查核鍵且回傳座標通過範圍檢查；`manual_coordinate` 核對經緯度範圍
- [x] d06 合成案例：每個進入原因各一列、等待中的查詢不進入、`located` 不進入、`conflict` 進入但暫停縣市不進入、三種判定各一、同檔重複查核鍵拒絕、檔名雜湊不符拒絕、`orphaned`、`stale`、同一組判定檔重跑結果不變、較晚判定取代較早判定
- [x] d07 全套測試、紀錄、提交

### D-2 回補與受影響月份（R10-1b）

回補指把 TGOS 採用與人工 `adopted` 判定的座標放進公開輸出。依 10-12 決定，回補只新增列；經新規則核驗的結果可更新既有列的地址與座標。

- [ ] d08 接法：新增 `final-state` 階段，位於 review 之後、output 之前。以 offline-state 為底，加上 tgos-state 的採用結果與 review 的 `adopted` 判定，產生 output 要讀的定位結果與 `coordinate_sources.parquet`。output 改讀 `final-state`，`output_id` 的雜湊輸入加入它的 manifest。若B 段 已讓 output 直接讀 tgos-state，改為在同一入口加入人工判定，不重做。10-18 已完成 `build_final_state` 與 `write_output(final_state=...)`；接 `run-full` 階段表與 `output_id` 等B 段 b12 到 b13
- [x] d09 來源優先序（10-18 擁有者決定）：門牌已有有效 `adopted` 判定、之後 TGOS 又定位時，留在第二類人工查核區，進入原因 `tgos_manual_conflict`。兩座標在 30 m 內採 TGOS；超過時記 `conflict`，兩邊都不採用
- [x] d10 受影響月份判定：比較基準是上一個公開版本的 `final-state`，第一次回補時是 offline-state。找出定位結果有差異的門牌鍵，經地址成員找出全部 `transaction_id` 加 `door_index`，取 `tx_yyyymm` 與 `category`，受影響檔案為這些年月與類別的每月 CSV 加 GeoParquet 與 `catalog.json`。年月取輸出分檔的 `trade_date` 年月（10-18 擁有者決定）。清單寫入 `final-state` 階段快照的 `affected_months.json`，含每個年月與類別的新增列數與更新列數
- [x] d11 未受影響月份雜湊不變的證明：一律重產全部輸出，比較新舊 `catalog.json` 逐檔。不在清單的檔案 `sha256`、`rows`、`size_bytes` 全部相同；在清單的檔案新列數減舊列數等於記錄的新增列數；清單有但雜湊沒變的檔案列為警告
- [ ] d12 合成 `run-full` 三種情況：無判定、一個 `adopted` 判定、一個新增 TGOS 採用。斷言受影響清單正確、未受影響 CSV 雜湊不變、`coordinate_sources.parquet` 列數等於更新門牌的觀測數。10-18 已在階段層級以合成資料測過三種情況；`run-full` 層級等接上後補
- [x] d13 全套測試、紀錄、提交

### D-3 邊界情況（R10-2）

| 情況 | 規格 | 合成案例 |
| --- | --- | --- |
| 跨年跨類別 | 一個門牌鍵同時出現在不同年月、不同類別。採用後每個年月與類別都列入受影響清單 | 同鍵出現在 2015 年買賣與 2024 年租賃 |
| 門牌重編 | 舊門牌與現行門牌沒有證據就不連結。舊門牌查無時留在第二類人工查核區，不以新門牌座標代替 | 舊號在地址來源不存在，TGOS 回傳新號 |
| 別名撤銷 | 別名撤銷後，經該別名定位的門牌不再定位。這會使已發布的列消失，擁有者未決定是否允許。決定前只偵測與報告：影響列寫入 `affected_months.json` 的 `removed`，回補停下，不自動刪列 | 撤銷前後各跑一次 |
| 晚到回傳 | `submission_unknown` 批次的回傳較晚交回時照常匯入。同一查詢較晚的成功結果與先前座標不同時記 `conflict`。C 段 每天到的批次就是這類案例 | 同查詢先查無後成功；先成功後不同座標 |

- [ ] d14 上表每個案例都有測試，受影響月份與新舊狀態符合證據，未證明地址等價時保留隔離
- [ ] d15 全套測試、紀錄、提交

### D-4 回補驗收（V-13）

以真實資料執行一次回補，紀錄放 `docs/records/V-13回補驗收.md`，腳本與產物放 `data/tmp/v-13/`。

- [ ] d16 回補關聯：`coordinate_sources.parquet` 每列都對應到輸出中存在的 `transaction_id` 加 `door_index`；每個 `adopted` 判定恰好套用一次
- [ ] d17 版本差異：新舊 `catalog.json` 逐檔比較，新增與更新列數等於受影響清單
- [ ] d18 受影響輸出：有變化的檔案集合等於清單，或差異已列為警告並說明
- [ ] d19 未受影響雜湊：清單外的每月 CSV 雜湊、列數、大小全部相同
- [ ] d20 冪等：同一組判定檔重跑一次，`output_id` 與全部檔案雜湊相同
- [ ] d21 計數：`orphaned`、`stale` 判定數，以及各進入原因的列數

## 等擁有者決定的事

| 事項 | 暫行做法 | 時機 |
| --- | --- | --- |
| 別名撤銷使已發布的列消失，允許嗎 | 偵測後停下，不自動刪列 | D-3 開工前 |

## 程式事實與行號

- [x] d22 10-18 以 HEAD `e9be8d7` 查定，haiku 子代理查、Claude 抽查 14 條相符。B 段會改 `pipeline.py`，施做前仍先 `grep` 確認

| 事實 | 位置 |
| --- | --- |
| `RUN_FULL_STAGES` 沒有 TGOS，也沒有人工判定 | `lvr_pipeline/pipeline.py:314-315` |
| `run_full` 的 review 呼叫不傳 `tgos_state`；output 只讀 converted 與 offline-state | `pipeline.py:652`、`:653` |
| `output_id` 只由 converted 與 offline-state 的 `manifest.json` 雜湊加字面值 `22-column` 算出 | `pipeline.py:529-531` |
| `build_review` 已有選用參數 `tgos_state`、`prior_review`、`run_id` | `lvr_pipeline/results/review.py:276-285` |
| review 階段目錄與輸出：`exception_addresses`、`cross_check_addresses` 的 Parquet 與 CSV | `review.py:326`、`:330-334`、`:436-438`、`:456-458` |
| 待查核碼 19 個，沒有查無、文字算不回鍵、缺區無候選的碼 | `lvr_pipeline/contracts/schemas.py:328-352` |
| TGOS 拒絕訊息對待查核碼 8 對 | `review.py:86-97` |
| 回傳列狀態 succeeded、failed、rejected；晚到成功座標與先前不同記 `conflict` | `lvr_pipeline/tgos/validate.py:282`、`:305`、`:349-352` |
| 可重送狀態；核准重送要全部合格且排在最前 | `lvr_pipeline/tgos/batches.py:498`、`:489-504`、`:553-555` |
| `--retry-query-fingerprint` 每個指紋一個旗標 | `lvr_pipeline/cli.py:98`、傳入 `pipeline.py:207` |
| 缺區地址沒有門牌鍵，不進 `unique_addresses`，留在出現列 | `lvr_pipeline/addresses/pool.py:354`、`:372`、過濾 `:267` |
| `catalog.json` 每檔記 `rows`、`size_bytes`、`sha256` | `lvr_pipeline/output/catalog.py:45-47` |
| 每月 CSV 路徑 `csv/YYYY/taiwanlvr_YYYYMM_<類別>.csv`；GeoParquet 在 `release/taiwanlvr.parquet`，release 目錄必須為空 | `lvr_pipeline/output/build.py:74`、`:55`、`:47` |
| 程式中沒有 `backfill`、`final-state`、`review-decisions`、`second_review` | 全部 not found |
| `RunStore`、`stage`、`reusable`、`bindings` 與程式指紋檔案清單 | `lvr_pipeline/storage/runs.py:381`、`:415`、`:436`、`:485-497` |

## 陷阱

- 判定檔是擁有者證據，程式只讀不寫。測試用合成判定檔放在 `tests/` 暫存目錄，不寫入 `data/review-decisions/`。
- 判定只決定座標與代表地址，不改 `building_key`、不合併門牌鍵。異體字或村改里即使擁有者採用座標，兩個鍵仍是不同門牌。
- 受影響月份的比較基準與新 run 必須是同一程式指紋。B 段 的全量重跑用新距離精度，所有月份都可能變，那是新基線，不是回補，不能用來證明未受影響月份不變。
- 不得刪除 tgos-state 中間快照或 `data/tgos/` 交換證據，不得修改已寫入的判定檔。
- `data/runs/` 的基線 run 只讀，回補使用新的 run ID。
- 合成測試若因新檢查失敗，改用正確夾具，不加繞過旗標。
- `import-tgos` 不保存被拒絕回傳列的座標（`tgos/validate.py` 拒絕時座標記空）。在這之前，`accept_tgos_response` 判定在真實帳本上一律記 `stale`，只有 `manual_coordinate` 能套用。保存原座標屬 `tgos/` 範圍，要在 D-4 之前由B 段或C 段補上，或由擁有者決定 V-13 只用 `manual_coordinate`。
- `rejected` 判定的門牌不進下一輪 TGOS 自動候選（契約轉換規則 4）要改 `prepare-tgos`，屬C 段 c32 到 c34 的範圍。
