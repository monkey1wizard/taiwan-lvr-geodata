# TGOS 批次重建紀錄

本次依擁有者更正，移除地址批次與日期的資料繫結。舊 TGOS 工作狀態及交換批次已刪除，並從全歷史離線快照重新產生交換檔。

## 範圍與版本

| 項目 | 填寫內容 |
| --- | --- |
| 日期、執行者 | 2026-10-07，Codex，Asia/Taipei |
| 工作 R 編號、驗收 V 編號 | 快速資料路徑及 R-09 前置修正。不是 V-12 完整驗收 |
| 程式提交、規則與契約版本 | 基底提交 `7b8a195` 加本次未提交修改。地址鍵 `building_key_v2` |
| 環境、工具版本 | Windows 11，專案 `.venv` Python |
| 輸入位置、固定版本、SHA-256 | `data/work/offline-state/snapshots/fast-all58-v5-offline`，manifest SHA-256 `95ffc9edc1c8a7c0101b7d999e37cfd1a5869b4b7e237d245876d17600a2bb98` |
| 選定批次、類別、月份、截止日期 | 全歷史離線狀態中的未命中地址。日期紀錄為 `2026-10-07`，只用於輸出資料夾名稱 |
| 未包含範圍 | 人工上傳、TGOS 回傳、匯入、回補及 V-12 完整驗收 |

## 命令與結果

| 命令與工作目錄 | 預期 | 實際 | 結束代碼 | pass／fail／not-run | 證據位置與雜湊 |
| --- | --- | --- | --- | --- | --- |
| `python -m pytest tests/test_p3_tgos.py tests/test_p4_address_patch.py -q` | TGOS 與地址 patch 合成測試通過 | 14 passed。加入日期紀錄測試後為 15 項 | 0 | pass | 本紀錄執行階段輸出 |
| `python -m pytest -q` | 全部合成測試通過 | 229 passed、1 failed。失敗是 `test_p0_sources.py` 仍預期已被取代的地址來源提交 `02887978` | 1 | fail | 地址來源目前固定為 `3ff9be0`，另行修正並重跑該測試 |
| `python -m pytest tests/test_p0_sources.py tests/test_p3_tgos.py tests/test_p4_address_patch.py -q` | 修正來源提交預期後，來源、TGOS 與地址 patch 測試通過 | 19 passed | 0 | pass | 本紀錄執行階段輸出 |
| `python -m lvr_pipeline prepare-tgos --state data/work/offline-state/snapshots/fast-all58-v5-offline` | 從乾淨離線狀態產生新交換批次 | 完成一批 10,000 筆 | 0 | pass | `data/tgos/2026-10-07/tgos-0358ee6e90df/addresses.csv`，SHA-256 `a6b3cc96d73ab95ddbf1d88ec2ac2c45ee86ee38d89ddcc805508e2ea832f734` |
| `python -m lvr_pipeline verify-tgos-state --input data/work/tgos-state/snapshots/tgos-state-ee379a73762041dbf40978b6` | 新狀態結構與關聯通過 | `verified: true`，批次數 1 | 0 | pass | 新 TGOS 快照 |
| CSV、manifest 及 Parquet 欄位核對 | 日期只存在獨立紀錄及資料夾名稱 | CSV 10,000 筆且地址不重複。manifest 與 `tgos-batch` 沒有日期欄位 | 0 | pass | 同上 |

## 資料核對與資源

新批次 ID 為 `tgos-0358ee6e90df`。批次 ID 由來源與地址內容產生，不含日期。CSV 共 10,000 筆，唯一地址數也是 10,000。新狀態只有這一個批次，沒有承接舊批次、舊匯入結果或舊別名。

## 結論與限制

日期現存於忽略 Git 的 `data/tgos/date.json`，內容只有 `date` 欄位。程式只使用該日期建立交換檔的上層資料夾。日期不限制地址挑選、提交或匯入。

舊 `data/work/tgos-state`、`data/work/fast/tgos-state` 及舊 `data/tgos` 批次內容已刪除。它們是本機生成狀態，未納入 Git。本次尚未人工上傳新交換檔，也沒有 TGOS 回傳可供匯入。
