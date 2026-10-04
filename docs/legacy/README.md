# 舊文件參考索引

本目錄只供新版企劃與文件重寫時參考。四份舊文件保留原文，不代表新專案的正式規格或可執行操作流程。

來源為 `C:\Code\taiwan-lvr-geojson`，複製日期為 2026-10-04。來源 Git HEAD 為 `5be26dc3e8b3a6425abb9b56f8fe4b9d9f8e187c`。

## 可再利用的內容

| 文件 | 原始位置 | 參考用途 |
| --- | --- | --- |
| [PROCESSING.md](PROCESSING.md) | `docs/PROCESSING.md` | 地址處理、交易分流、多門牌與人工補字規則 |
| [DATA_SOURCES.md](DATA_SOURCES.md) | `docs/DATA_SOURCES.md` | 資料來源、下載方式與來源授權紀錄 |
| [RESUBMIT_RUNBOOK.md](RESUBMIT_RUNBOOK.md) | `docs/RESUBMIT_RUNBOOK.md` | TGOS 人工操作、座標系與回傳欄位 |
| [CONTRACT.md](CONTRACT.md) | `CONTRACT.md` | 舊輸出欄位、交易識別方式與前端介面 |

## 已知過時內容與差異

- SQLite 快取、舊定位成果回灌與一次性 migration 不符合新版草稿。
- 預先切完多份 TGOS 檔案的方式不符合新版草稿。草稿預計每輪匯入後重建 unresolved pool。
- 舊 `unresolvable.csv` 使用永久排除方式。新版草稿則預計讓未定位地址持續接受歷史回補。
- `DATA_SOURCES.md` 記載「無額度限制」。同目錄其他文件記載每日 10,000 筆，兩者互相矛盾。重寫時需確認當時適用的服務限制。
- 舊文件引用的 `lvr_pipeline.run`、TGOS 匯入、GIS 輸出與其他腳本不在本 repo。不要直接執行舊文件指令。
- `PROCESSING.md` 將部分匯入及輸出步驟標示為尚未實作，不能據此判定舊流程完整。
- 舊相對連結、記憶筆記與計畫路徑可能不存在。原文保留供查考。
- `CONTRACT.md` 的輸出格式與線上資料表是舊版設計，尚未定為新版契約。
- 舊文件中的資料筆數、涵蓋縣市、第三方來源與服務能力只反映當時紀錄，尚未重新驗證。

## 後續使用方式

重寫文件時，先核對保留程式碼與實際來源，再取用仍適用的內容。新版企劃目前放在 [docs/drafts/](../drafts/taiwan-lvr-geodata-新版資料處理流程.md)，仍是待重寫草稿。
