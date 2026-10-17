# D 段接入 run-full

## 範圍與版本

| 項目 | 填寫內容 |
| --- | --- |
| 日期、執行者 | 2026-10-18，Asia/Taipei。Claude 拆卡、抽查、退回兩項預設值並撰寫文件；sonnet 子代理先在乾淨複本 `data/tmp/d/head` 寫程式與測試，再到工作區修正並跑全套測試 |
| 工作 R 編號、驗收 V 編號 | R10-1a、R10-1b，[D 子企劃](../plans/D-人工查核與回補.md) d02、d08、d12，以及 D-3 的 d15。不屬於任何 V 驗收 |
| 程式提交、規則與契約版本 | 基於 `fdd2792`，含B 段 B-4 `f56558c`。本卡提交見 git log |
| 環境、工具版本 | Windows 11、Python 3.14.8（`.venv`）、DuckDB 1.5.6 |
| 輸入位置、固定版本、SHA-256 | 只用合成輸入。`pipeline.py` `cb354d68…`、`second_review.py` `c118df32…`、`final_state.py` `c1e4ddcb…`、`test_d_run_full.py` `69df4eae…` |
| 選定批次、類別、月份、截止日期 | 不適用，合成測試 |
| 未包含範圍 | 真實資料在 D-4，要等B 段 b16 過渡基線 `b16-20261018a` |

## 命令與結果

| 命令與工作目錄 | 預期 | 實際 | 結束代碼 | pass／fail／not-run | 證據位置與雜湊 |
| --- | --- | --- | --- | --- | --- |
| 乾淨複本 `data/tmp/d/head`（`f56558c` 匯出）中跑全套 | 全部通過 | 1 failed、969 passed、10 errors。11 筆都是複本缺 `.gitignore` 與 `.python-version` 所致，與本卡無關 | 1 | fail | `data/tmp/d/pytest_wire_full.log` |
| 工作區 `test_d_run_full.py` 加 `test_final_state.py` | 全部通過 | 19 passed，69.82 s | 0 | pass | 子代理回報 |
| 工作區 `test_second_review.py`、`test_d3_edge_cases.py`、`test_review_exceptions.py`、`test_r08_2a_maintenance.py`、`tests/acceptance/test_run_full.py`、`test_r07_3b_run_full_output.py` | 全部通過 | 77 passed，193.51 s | 0 | pass | 子代理回報 |
| 工作區全套 `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=C:/Code/taiwan-lvr-geodata/data/tmp/d/pytest-wire2-full`，HEAD `fdd2792` 加本卡改動，工作區沒有其他 session 的未提交改動 | 全部通過 | 986 passed，923.24 s | 0 | pass | `data/tmp/d/pytest_wire2_full.log` |

## 實作

| 項目 | 位置 |
| --- | --- |
| `RUN_FULL_STAGES` 加入 `final-state`，位於 review 之後、output 之前 | `lvr_pipeline/pipeline.py` |
| review 階段傳入 tgos-state、離線索引與判定檔目錄；判定檔或比較基準改變時重建 review 或 `final-state` | 同上 |
| `output_id` 加入 `final-state` 的 manifest 雜湊；run report 加入 `review_decisions`、`second_review`、`final_state` 三段 | 同上 |
| CLI 旗標 `--review-decisions`、`--backfill-baseline` | `lvr_pipeline/cli.py`、`pipeline.py` `_run_full_command` |
| `response_lng`、`response_lat` 改讀帳本原始座標欄；新進入原因 `tgos_keyless_query` | `lvr_pipeline/results/second_review.py`、`contracts/schemas.py` |
| `final-state` 與 output 改以 tgos-state 為底 | `lvr_pipeline/results/final_state.py`、`output/rows.py` |
| 維護包帶 `final-state` 快照 | `scripts/maintenance.py` |
| 合成 `run-full` 三種情況、CLI 與 API 預設目錄、重跑冪等 | `tests/integration/test_d_run_full.py` |

## 資料核對與資源

只用合成輸入，不涉及來源列數或磁碟用量。d12 三種情況都使用 `run-full`：無判定、一個 `manual_coordinate` 判定、一個新增 TGOS 採用。每種情況都核對：

- `affected_months.json` 正確。
- 對無判定 run 的 `catalog.json`，`compare_catalogs` 通過且沒有警告。
- `coordinate_sources.parquet` 列數等於更新門牌的觀測數。

同一 run ID 重跑時每個階段都重用，`output_id` 不變。換新 run ID、輸入相同時，輸出檔與 `catalog.json` 位元組相同，但 `output_id` 不同，因為各階段 manifest 含耗時欄位。

## 結論與限制

已完成：d02、d08、d12 的程式與合成測試，以及 d15 的全套測試。尚未做：真實資料驗收（D-4）。

- 抽查時退回兩項預設值，都由子代理修正：
  - 子代理依 Claude 先前的錯誤指示，把比較基準預設成本次 tgos-state。依企劃，第一次回補應比較 offline-state，否則 TGOS 採用不會算成新增，所以已改回。
  - `run_full` 原本會自行讀取 `data/review-decisions/`，這樣合成測試會讀到擁有者的真實判定檔。已改為只有 CLI 會套用這個預設目錄。
- 已送出、但在現行規則下算不出門牌鍵的查詢，第 1 批有 181 筆。這些查詢以 `tgos_keyless_query` 進入第二類人工查核區。它們沒有觀測關聯，所以任何判定都記 `stale`。
- 換 run ID 時 `output_id` 會變，所以 d20 的冪等核對要在同一個 run ID 下重跑，或改比較輸出檔雜湊。
