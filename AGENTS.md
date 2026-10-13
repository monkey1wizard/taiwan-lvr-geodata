# 執行契約

## 現況與文件入口

- 以 [README](README.md) 作為唯一使用入口，禁止新增操作手冊或子目錄 README。
- 使用 [重建企劃](docs/plans/重建企劃.md)查看唯一現況、R 工作依賴、V 驗收及待補契約。地址專案匯入見[地址專案匯入企劃](docs/plans/地址專案匯入企劃.md)。`.dev` 與 GAL 狀態不是專案現況來源。
- 文件權責、行數限制與同步方式見 [CONTRIBUTING](CONTRIBUTING.md)。正式文件不得超過 550 行，建議 450 行內。不得壓縮文字或刪減需求以規避上限。歷史 records 與 archive 不受限制，新驗收紀錄不得超過 550 行。
- [輸出欄位](docs/輸出欄位.md)是新版公開輸出的唯一規格權威文件。企劃只追蹤規格狀態與待驗收工作，不複製欄位表。
- 從 [空白結果範本](docs/task-result-template.md)複製新驗收紀錄到 `docs/records/`，每卡或每次驗收各一份。填寫命令與 pass／fail／not-run。範本必須保持空白。舊紀錄的結論依當時版本保留。
- `docs/archive/` 保存被取代文件原文，並須清楚標示歷史內容不作現行規範。

## 執行與相容性

- 使用 `C:/Code/taiwan-lvr-geodata` 作為權威工作區。在 `main` 修改、完成適用驗收、本機提交。不推送 `origin/main`，也不詢問推送。除非擁有者明確要求，不建立工作副本、階段分支或 PR。
- 不回復其他人的變更。開始工作前確認 Git 狀態及檔案變更歸屬。
- Linux 已安裝 uv 後執行 `bash scripts/setup.sh`。安裝與一般測試使用合成輸入，不需要憑證，也不得下載真實資料。讀取本機 `data/raw/` 與 `C:/Code/taiwan-address-data` 不需擁有者同意；真實資料命令另記輸入、雜湊與結果。GitHub Actions 提供 Linux 驗證。
- 2026-10-12 起，既有衍生資料不作新流程輸入、定位依據或驗收基準。包含 normalized／converted、地址池／index／state、aliases、TGOS 採用結果、review 裁定、衍生 cache、舊快照、維護包、公開輸出及統計。本機只保留 `data/raw/` 的原始 ZIP，未重核前不能視為正確來源或自動納入。舊 TGOS 回傳結果已於 2026-10-13 由擁有者決定廢棄，不重核、不納入任何新結果；本機已無這些檔案。
- 新 run 必須使用重新核對的來源清單、原始 ZIP 與地址來源，獨立建立完整輸入清冊。快照只可在本次新 run 內經新規則驗證後重用。程式隔離護欄已實作（R07-6a）：無本次 `rebuild_epoch` 的快照與 run 會被拒絕，`run-full` 只接受本次紀元重新掃描產生的來源清冊；人工 `verified` 簽核已於 2026-10-13 取消，程式檢查由 R07-6c 移除。舊公開輸出目錄與維護包已隨 R07-8 刪除舊路徑；TGOS `--ledger` 轉接不得使用，待 R09-2 移除。
- 保留 `validated_roc_to_tx_yyyymm`。門牌鍵唯一介面為 `building_key`，規則版本存 metadata。2026-10-12 已完成名稱遷移：程式只剩 `building_key` 單一路徑，舊演算法、`building_key_v2` 與 `legacy_building_key` 已移除。不得重新加入 alias、wrapper、雙軌或舊結果 fallback。依來源規格以獨立案例重驗地址規則仍未完成，R07-6b 尚未接受。一般無關 CLI 入口不因此要求刪除。
- 舊 TGOS 帳本與舊回傳在本機已不存在，不轉接、不擋重送；新重建以新帳本從零開始，離線定位不到的門牌全部重新送查。新帳本在本次重建期間不得清空或繞過，`submission_unknown` 不得自動重送。
- 不推斷交易身分、修訂順序、子門牌等價、座標軸或來源權限。身分未獲證明時保留來源觀測。

## 資料與檢查產物

- 檢查、驗證、診斷、審查輸出與需保留的失敗產物放在 `data/tmp/<purpose>/`。
- `data/raw/`、`data/cache/`、`data/tgos/` 與 `data/output/` 是目標布局資料位置。`data/output/` 是公開 Git 下載位置。`.gitignore` 已於 R07-9 改為忽略 `data/*` 但不忽略 `data/output/`，所以 `data/output/` 下的檔案會被 Git 追蹤；只有經發布流程核准的公開輸出應放進該目錄。
- 秘密、raw ZIP、run／stage 快照及第三方地址來源列不得加入 Git。
- 不因缺檔而假設來源是空批次，不覆寫原始證據，也不以實作完成推定驗收通過。
- 不要在 P0 範圍發布或更新相關地址資料專案。
