# B-4 `run-full` 讀帳本

## 範圍與版本

| 項目 | 填寫內容 |
| --- | --- |
| 日期、執行者 | 2026-10-18，Asia/Taipei。Claude 拆卡、契約與抽查，sonnet 子代理實作 |
| 工作 R 編號、驗收 V 編號 | B 子企劃 b12 至 b15 |
| 程式提交、規則與契約版本 | 本紀錄所在提交。契約：[資料契約](../資料契約.md)「TGOS 帳本快照與 `run-full` 帶入」，`7375669` |
| 環境、工具版本 | Windows 11、`.venv` Python、DuckDB |
| 輸入位置、固定版本、SHA-256 | 合成資料。唯讀抽查：`data/runs/r09-7-20261017/tgos-state` 現行快照 `tgos-state-443e6121736dc583c5c89082`，`manifest.json` `c019d4dc…`；第 1 批回傳 `data/tgos/20261017-7c3aca87/Address_Finish.csv` `6289a5a6…` |
| 選定批次、類別、月份、截止日期 | 不適用 |
| 未包含範圍 | 地址池以 `tgos_district_fill` 補區延後到 00 主企劃 e00。真實全量 run 在 B-5 |

## 命令與結果

| 命令與工作目錄 | 預期 | 實際 | 結束代碼 | pass／fail／not-run | 證據位置與雜湊 |
| --- | --- | --- | --- | --- | --- |
| 子代理：相關測試檔，repo 根目錄 | 全部通過 | 562 passed、187 passed、20 passed | 0 | pass | 子代理回報 |
| Claude：全套測試，在 `git archive HEAD` 加 B-4 工作區檔案的乾淨複本 `data/tmp/b/head` | 只剩已知項目 | 16 failed、925 passed、29 errors，分類見下節 | 1 | fail | `data/tmp/b/pytest-b4.log` |
| `.venv/Scripts/python.exe data/tmp/b4/carry_check.py`，唯讀 | 帳本可帶入，回傳雜湊相符 | 見下節 | 0 | pass | `data/tmp/b4/carry_check_output.txt` |

## 資料核對與資源

全套測試的失敗分三類：

| 類別 | 數量 | 處理 |
| --- | --- | --- |
| 乾淨複本缺 `docs/`、`.gitignore`、`.python-version` | `test_r07_8_output` 1、`test_r07_9_catalog_verify` 1、`test_r08_4_verify_release` 10 | 不是程式問題，B-7 的乾淨複本改含這些檔案 |
| 維護包仍讀 offline-state | `test_r08_2a_maintenance` 10 | 已修：維護包改帶 converted 與 tgos-state，修後 20 passed |
| plan-d 的 D 段程式與測試仍讀單獨匯入的完整快照與 offline-state | `test_final_state` 9、`test_second_review` 14 | 交給 plan-d：改用 `merge_ledger` 或 run 的 tgos-state |

帶入第 1 批帳本的唯讀抽查：25 批中 1 批 completed 改回 submitted，12 批 cancelled、12 批 prepared 不變。236,631 筆查詢中 6,330 筆依 `v2.7` 重算鍵，181 筆在現行規則下無鍵，全部在第 1 批，例如 `臺北市大安區信義區四段267號11樓`，依選項 A 保留為證據。第 1 批回傳檔雜湊等於帳本紀錄。

新介面：
- `tgos-result` 新欄 `response_lng`、`response_lat`。
- 新報告計數 `tgos_carried_keyless_queries`、`carried_query_keys_recomputed`、`tgos_reimported_batches`。
- 無鍵已送查詢的 `query_kind` 為 `keyless`。
- `merge_ledger(source, work_dir, merged_dir, *, run_id=None)` 從帳本快照寫出完整合併快照。
- `revoke-alias` 改用 `--runs-root` 與 `--run-id`。

## 結論與限制

- 程式實作完成，B 段相關合成測試通過。提交當下 plan-d 的 23 個測試失敗，已通知 plan-d。
- 延後：地址池補區與 offline-state 規則版本記錄，見 00 主企劃 e00。無鍵已送查詢進待查核需要 `review.py` 對應，屬 D 段。
- 每次 run 帳本會被雜湊兩次，真實帳本約 25 MB，影響小，未最佳化。
