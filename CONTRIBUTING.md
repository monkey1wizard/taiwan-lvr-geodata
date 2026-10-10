# 開發、發布與復原

開發流程、文件維護、發布與復原集中於本文件。資料來源及處理規則見[資料架構](docs/architecture.md)，下載入口見[README](README.md#下載資料)。

## 開發指南

在 `C:/Code/taiwan-lvr-geodata` 的 main 開發。重建工作以[重建企劃](docs/plans/重建企劃.md)的 R 編號與 V 驗收條件為準，GAL 與本機 `.dev` 檔案為選用。不要沿用舊 T 編號的完成結論。

### 現行架構與目標架構

| 現行位置 | 責任 | 重建方向 |
| --- | --- | --- |
| `lvr_pipeline/` | 解析、正規化、地址處理、輸出及 CLI | 依企劃拆分責任，共用核心規則 |
| `lvr_pipeline/contracts/` | JSON 與 Arrow 契約 | 收斂至 contracts |
| `config/sources/`、`config/reference/`、`config/rules/` | 來源描述與小型規則 | 已由 `data/` 移入（R03-9） |
| `data/work/` | 既有階段快照 | 定義版本及續跑契約後遷移 |
| `scripts/`、`tests/` | 安裝、驗證與合成測試 | 保留有效驗證，按責任整理 |

本次只整理文件，未建立目標程式骨架。目標目錄、模組責任與工作依賴在企劃維護。面向讀者的完整資料處理說明集中在[architecture.md](docs/architecture.md#完整資料處理流程)，流程改動時須同步核對。

### 開發與驗證

安裝方式見[使用指南](docs/architecture.md#使用與資料處理)。Linux 執行 `bash scripts/setup.sh`，GitHub Actions 負責 Linux 驗證。Windows 通過不能代替 Linux 驗收。

自動測試只使用合成輸入，不下載真實資料，不使用憑證。真實全量、TGOS 人工交換與公開發布各自記錄結果，不能從合成測試通過推定完成。

保留 `0_parse_raw.py`、`1_normalize.py`、`address.py` 等必要相容入口，以及 `building_key_v2`、`validated_roc_to_tx_yyyymm` 契約。搬移模組前先確認匯入、命令及套件資源相容性。

工作在 main 修改、測試並本機提交，再直接推送 origin/main。除非另有明確要求，不建立開發副本、階段分支或 PR。秘密、raw ZIP、生成快照及第三方地址資料列不得進 Git。

### 文件位置與維護責任

| 位置 | 用途 |
| --- | --- |
| 根目錄 README | 唯一專案入口 |
| docs/architecture.md | 來源、操作、資料規格、地址規則及 TGOS |
| 根目錄 CONTRIBUTING.md | 開發、驗證、發布與復原 |
| `docs/plans/` | 唯一現行重建企劃 |
| `docs/records/` | 問題證據、遷移及實際驗收紀錄 |
| `docs/archive/` | 已取代文件，不作現行執行依據 |
| [結果範本](docs/task-result-template.md) | 空白格式，實際結果另存 records |

子目錄不建立 README。現行說明每個主題只有一個規則來源，其他文件連結引用。

舊 DATA_SOURCES.md 已併入 architecture.md，不另保留轉址。歷史來源描述的原路徑對應封存原文，程式產生的文件引用待 R-03 更新。

### 工作結果

複製空白範本到 records，再填命令、版本、輸入雜湊、預期／實際、結束代碼及 pass／fail／not-run。不得把實際結果寫回範本。

文件、程式實作、合成測試、真實資料驗收及公開發布各有不同完成條件。舊階段記錄與證據 JSON 保留原有範圍，不能直接算作 R 工作包的新版驗收。

## 發布與復原

只有輸入範圍、定位語意及輸出驗證均通過的候選才能發布。現有 TGOS 錯配候選須先修復及重新驗證。本機公開指標仍為單季離線版本，不能據此宣稱全歷史已發布。

### 現有位置與打包

目前發布候選在 `data/output/`，小型版本描述與指標在根目錄 `releases/`。

`package-output` 以 converted 輸入、定位 state、來源 notices 及 run ID 建立成品。`verify-output` 檢查產物。實際參數以各命令 `--help` 為準，避免把封存範例中的舊 run ID 當成新版本。

發布驗收至少涵蓋：

- 選定、缺失及空批次，來源觀測的保留、排除、失敗數量。
- 三種格式的 ID、屬性、筆數、null 與幾何一致性。
- 年度包成員與原月檔雜湊，全部附件及分片索引。
- 可重產的維護包、來源顯名及限制。
- 真實全量資源量測與磁碟餘裕。

全歷史規則見[重建企劃](docs/plans/重建企劃.md)。來源條件與顯名必須保留，不重新加入已取消的上游權威證明門檻，也不自行推定來源權限。

### 發布與指標更新

先固定來源提交與候選產物，再核對預期的 main 父提交。上傳附件後，核對下載檔案的雜湊與版本內容。公開指標只有在新版本驗證完成後才能更新。

現有 `publish-output` 及 `commit-release-pointer` 分別處理發布與指標提交。後者涉及 Git 提交及推送，不是唯讀驗證命令。發布前檢視目前 CLI 參數，記錄 expected parent、receipt 及目標版本。

附件大小、數量、分片與不可變發布設定須在實際發布時重新核對。封存文件中的平台限制是當時紀錄，不當成永久不變的現行值。

若附件、receipt 或預期父提交不一致，停止更新指標並保留前版。不得強制推送、覆寫原始證據或替換已使用的版本標籤來掩蓋失敗。

### 無原始 ZIP 的維護交接

使用 `fetch-output --maintenance` 取得該版維護包。其餘下載參數見[資料下載](README.md#下載資料)。

維護交接包含 `handoff.json`、converted 資料、state 及狀態階段。先核對清單中的相對路徑、檔案大小與雜湊，再判定狀態是 offline-state 或 tgos-state，不能自行重設帳本。

既有 `verify-public-snapshot` 子命令（`python -m lvr_pipeline verify-public-snapshot`）用於指定 manifest URL 與雜湊的公開快照重產驗證。正式驗收必須在無 raw 的乾淨 Linux 環境執行。舊單季驗證通過不代表新版全歷史通過。

### 失敗復原

保留失敗候選、命令、結束代碼及診斷。重新執行前核對輸入與規則綁定，不能直接採用半成品。

若需要回到舊公開版本，先確認該版本附件仍可取得且雜湊正確，再以可追溯的新提交調整指標。不得改寫歷史。TGOS 帳本與原始回傳保留，復原輸出不代表配額恢復。

歷史公開與復原細節保存在[封存發布紀錄](docs/archive/release-runbook.md)及[雲端驗證紀錄](docs/archive/cloud-runbook.md)。本次整理未發布、推送或重新執行公開下載驗證。
