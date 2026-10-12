# 執行契約

## 現況與文件入口

- 以 [README](README.md) 作為唯一使用入口，禁止新增操作手冊或子目錄 README。
- 使用 [重建企劃](docs/plans/重建企劃.md)查看唯一現況、R 工作依賴、V 驗收及待補契約。`.dev` 與 GAL 狀態不是專案現況來源。
- 文件權責、行數限制與同步方式見 [CONTRIBUTING](CONTRIBUTING.md)。正式文件不得超過 550 行，建議 450 行內。不得壓縮文字或刪減需求以規避上限。歷史 records 與 archive 不受限制，新驗收紀錄不得超過 550 行。
- [輸出欄位](docs/輸出欄位.md)是新版公開輸出的唯一規格權威文件。企劃只追蹤規格狀態與待驗收工作，不複製欄位表。
- 從 [空白結果範本](docs/task-result-template.md)複製新驗收紀錄到 `docs/records/`，每卡或每次驗收各一份。填寫命令與 pass／fail／not-run。範本必須保持空白。舊紀錄的結論依當時版本保留。
- `docs/archive/` 保存被取代文件原文，並須清楚標示歷史內容不作現行規範。

## 執行與相容性

- 使用 `C:/Code/taiwan-lvr-geodata` 作為權威工作區。在 `main` 修改、完成適用驗收、本機提交，再直接推送 `origin/main`。除非擁有者明確要求，不建立工作副本、階段分支或 PR。
- 不回復其他人的變更。開始工作前確認 Git 狀態及檔案變更歸屬。
- Linux 已安裝 uv 後執行 `bash scripts/setup.sh`。安裝與一般測試使用合成輸入，不需要憑證，也不得下載真實資料。GitHub Actions 提供 Linux 驗證。
- 保留相容入口。新契約使用 `building_key_v2` 與 `validated_roc_to_tx_yyyymm`。
- 不推斷交易身分、修訂順序、子門牌等價、座標軸或來源權限。身分未獲證明時保留來源觀測。

## 資料與檢查產物

- 檢查、驗證、診斷、審查輸出與需保留的失敗產物放在 `data/tmp/<purpose>/`。
- `data/raw/`、`data/cache/`、`data/tgos/` 與 `data/output/` 是目標布局資料位置。`data/output/` 已決定作為公開 Git 下載位置，但目前 `.gitignore` 仍忽略整個 `data/`，追蹤行為待實作。
- 秘密、raw ZIP、run／stage 快照及第三方地址來源列不得加入 Git。
- 不因缺檔而假設來源是空批次，不覆寫原始證據，也不以實作完成推定驗收通過。
- 不要在 P0 範圍發布或更新相關地址資料專案。
