# C 第二輪送查準備

c34 從第二版基線 `e17b-20261028` 準備第二輪第 1 批送查檔，並依 c32 產生重送清單給擁有者決定。送查檔不由程式上傳，交給擁有者。

## 範圍與版本

| 項目 | 填寫內容 |
| --- | --- |
| 日期、執行者 | 2026-10-29，plan-c，Claude |
| 工作 R 編號、驗收 V 編號 | C 段 c32、c34。無對應 V 編號 |
| 程式提交、規則與契約版本 | `94fd0a9`，地址規則 `v2.11`，`door_rule = village_neighborhood_subid_v2` |
| 環境、工具版本 | Windows 11，`.venv` Python。`LVR_DUCKDB_MEMORY_LIMIT=8GiB LVR_DUCKDB_TEMP_LIMIT=64GiB LVR_DUCKDB_THREADS=4` |
| 輸入位置、固定版本、SHA-256 | tgos-state `data/runs/e17b-20261028/tgos-state/snapshots/tgos-state-2136832a9c2910abe4c70b8a`，manifest `28944b69…`。review `review-55bb62d56af14c835965d2f8`，manifest `24d46cdf…`。offline-index `offline-index-9605b53aa0b60ff42bc655bf` |
| 選定批次、類別、月份、截止日期 | 新批次 `tgos-fec1eef6ca7b` |
| 未包含範圍 | 核准重送。重送要擁有者依清單決定，本次不帶 `--retry-file`。上傳與匯入由擁有者執行 |

## 命令與結果

| 命令與工作目錄 | 預期 | 實際 | 結束代碼 | pass／fail／not-run | 證據位置與雜湊 |
| --- | --- | --- | --- | --- | --- |
| repo 根目錄：`data/tmp/c34/dry_run.py`，把寫入快照與交換檔的函式換成空函式後執行 `prepare_tgos` | 先看筆數，不寫檔 | 3,125 筆，`csv_sha256` `589ee46d…` | 0 | pass | `data/tmp/c34/dry_run.json` |
| repo 根目錄：`python scripts/tgos_retry_list.py --review <e17b review> --state <e17b tgos-state> --out data/tmp/tgos-round2/retry-candidates.csv` | 列出可重送的查無門牌 | 14,824 筆：門牌鍵 14,597、缺區 227。送查文字與上次相同 13,875 筆，改變 949 筆。文字算不回鍵 1 筆不列 | 0 | pass | `retry-candidates.csv` `792a0818…`，報告 `retry-candidates-report.json` `021493e2…` |
| repo 根目錄：`python -m lvr_pipeline prepare-tgos --state <e17b tgos-state> --index <e17b offline-index> --date-file data/tmp/tgos-round2/date-20261029.json --run-id c34-20261029` | 帳本從 e17b 接續，新批次只含從未送查的門牌 | 批次 `tgos-fec1eef6ca7b`，3,125 筆，與試跑相同 | 0 | pass | 快照 `data/runs/c34-20261029/tgos-state/snapshots/tgos-state-1b2a2c7b930db268f1fe1b0c`，manifest `30849a6e…`。`prepare.log` `0abae3f4…` |

## 資料核對與資源

送查檔在 `data/tgos/20261029-fec1eef6/`：

- `addresses.csv` 3,125 列加表頭，SHA-256 `589ee46de01de895d3c4c8aec4842c799961de4a0c1fa64ffd308504669fd9ed`，等於帳本 `csv_sha256`。
- `manifest.json` SHA-256 `1dfe956f531b2ee40401740e119fdf2bafc0801548f1ea116b9ab0a30d7cdb68`。

新批次全部是門牌鍵查詢：嘉義市 2,602 筆、新竹市 523 筆。缺區查詢 0 筆。只出現這兩個縣市的原因尚未查。

`prepare-tgos` 報告的其他計數：

- 已送查而不再選的門牌鍵 36,858 個，缺區門牌 1,153 個。
- 送查文字算不回鍵的門牌鍵 2 個，缺區 1 個。
- 缺區地址沒有候選區 1,991 個，不送。
- 缺區文字相同而合併 2,148 筆。

重送清單是擁有者決定的輸入，不是送查檔：

- 清單只列第二類人工查核區中 `tgos_not_found`、狀態 `pending` 或 `deferred` 的門牌。e17b 沒有 `rejected` 判定，`submission_unknown` 的門牌也是 0 個。
- `text_changed = yes` 的 949 筆在現行規則下會以新文字送出。`no` 的 13,875 筆會送出與上次相同的文字。
- 核准時，把要重送的列留在清單中，以 `prepare-tgos --retry-file <清單> --retry-reason <理由>` 準備。核准的重送排在批次最前面，一批最多 10,000 筆。

## 結論與限制

- 程式實作與合成測試：c32、c33 已在 `94fd0a9` 提交。
- 真實資料：第二輪第 1 批已準備，狀態是 `prepared`，未送出。
- 送出後的步驟同 C-1：`set-tgos-status --status submitted`，再 `import-tgos`，都用 run `c34-20261029`。
- 重送清單未經擁有者決定，所以這一批沒有帶重送。今天只送這一批時，剩下 6,875 筆名額不用。要和重送合成同一批，須先取消這批再重新準備，取消後交換資料夾要刪除。
