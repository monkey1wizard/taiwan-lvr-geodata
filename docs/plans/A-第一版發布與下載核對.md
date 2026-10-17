# A 第一版發布與下載核對

類別 iii：不論 TGOS 情況都能持續做。修訂日期：2026-10-18。現況與擁有者決定以 [00 主企劃](00-主企劃.md)與[擁有者決定與驗收總表](擁有者決定與驗收總表.md)為準。a01 到 a11 的核取方塊只在 00 主企劃，本檔寫每一步怎麼做、預期什麼、在哪裡停。

## 目的

把基線 `r07-10-20261016b` 的離線配對輸出發布為第一版，再從公開位置下載回來逐檔核對，完成 V-11 第一版。下載核對是 R08-4，紀錄在 `docs/records/R08-4下載核對.md`。

## 已經走到哪

| 日期 | 發生什麼 |
| --- | --- |
| 10-16 | 基線通過 V-10b 九項與 R08-2a 維護包重產 |
| 10-17 | 555 檔複製到 `data/output/` 並核對通過，分 17 個提交推送到 `origin/main`，最後一個是 `b21217e`。核對工具提交 `ee25d1c`，已在遠端 |
| 10-18 | `gh release list` 沒有任何 release。核對工具 8 個測試在 HEAD `e6b1c05` 通過。用本機資料做離線預演，寫 R08-4 紀錄草稿 |

## 預期值

下載核對的每一項都以這些值為準。來源是 [R08-3 紀錄](../records/R08-3第一版發布.md)與基線 `output.json`。

| 項目 | 預期 |
| --- | --- |
| 資料提交 | `b21217e`，`data/output/` 下 555 檔：554 個每月 CSV 加 `catalog.json` |
| `catalog.json` | 193,491 bytes，SHA-256 `5a0067ccb056437ebd09ee8483b9c6ba34599d0e23803b4ab213c88482081ba2` |
| `taiwanlvr.parquet` | 1,000,026,444 bytes，SHA-256 `3d5b851b38b465c423a446f73f75376ef58fa86857c08f252ff32780e7237857` |
| 列數 | 3,551,724 |
| 基線候選 | `data/runs/r07-10-20261016b/output/snapshots/output-d86374ae27720cfbb86d5fcf/output.json`，只讀 |

## 核對工具做什麼

`scripts/verify_release.py` 依序跑 10 項檢查，每項記 pass、fail 或 not-run。

| 檢查 | 內容 |
| --- | --- |
| `release_assets` | 以 `gh release view` 讀 release 的檔案清單，必須有 `taiwanlvr.parquet` |
| `download` | 以 `gh release download` 下載 parquet |
| `clone` | blobless clone 公開 repo，不展開工作目錄，解析資料提交 |
| `catalog` | `catalog.json` 的 SHA-256 等於預期 |
| `file_set` | 提交內 `data/output/` 的檔案集合與 catalog 完全一致 |
| `public_files` | 每個 CSV 的 SHA-256 與大小等於 catalog |
| `release_file` | 下載的 parquet 雜湊與大小等於 catalog，GitHub 回報的 digest 也要相符 |
| `parquet_meta` | parquet 列數等於 catalog，記錄欄數與 geo metadata |
| `candidate` | 下載檔的雜湊逐檔等於基線 `output.json` 的 `files_sha256` |
| `verify_output` | 只在 `full` 模式：展開 `data/output/`，對它與下載的 parquet 跑完整 `verify_output` |

`passed` 表示沒有任何 fail。`complete` 還要求每項都 pass，所以 `stream` 模式永遠不是 complete。

## 步驟

### release 存在前：預演與草稿

1. 跑 `tests/integration/test_r08_4_verify_release.py`。10-18 結果 8 passed。
2. 離線預演。轉接腳本 `data/tmp/r08-4/rehearse/rehearse.py` 只換掉 `run_gh`：檔案清單改為模擬，下載改為複製基線 parquet。clone 改從本機 repo，其餘照原腳本，`full` 模式加 `--candidate`。本機 repo 沒開 partial clone，clone 會退回完整複製，約 10 GB。
3. 從[空白範本](../task-result-template.md)複製 R08-4 紀錄草稿，寫入命令、預期值與預演結果，正式結果欄留空，本機提交。
4. 停下，等擁有者做 a05 與 a06 並告知 tag。

預演的 `release_assets` 與 `download` 是模擬，不算下載核對。預演只證明工具在真實規模的資料上能跑完。

### a08 本機完整核對

release 存在後在 repo 根目錄執行，產物在 `data/tmp/r08-4/`：

```bash
LVR_DUCKDB_MEMORY_LIMIT=8GiB LVR_DUCKDB_TEMP_LIMIT=64GiB LVR_DUCKDB_THREADS=4 .venv/Scripts/python.exe scripts/verify_release.py --tag <tag> --mode full --candidate data/runs/r07-10-20261016b/output/snapshots/output-d86374ae27720cfbb86d5fcf/output.json --work data/tmp/r08-4
```

報告在 `data/tmp/r08-4/report-<tag>-full.json`。通過條件是 `passed=true` 且 `complete=true`。任何一項 fail 就停下回報，不改腳本重跑。

### a09 GitHub 核對

前置：a06 已推送，`verify-release.yml` 出現在預設分支。觸發前先問擁有者一次。同意後執行：

```bash
gh workflow run verify-release.yml --repo monkey1wizard/taiwan-lvr-geodata -f tag=<tag>
```

runner 磁碟約 14 GB。第 1 層用 `stream` 模式。第 2 層先清出空間，可用達 35 GB 才跑 `full`。第 2 層沒跑時，summary 會寫明，紀錄也要寫明 `verify_output` 在 GitHub 上是 not-run。結果以 `gh run view` 與下載的 `verify-release-reports` artifact 為準。

### a10 紀錄與總表

把 a08 與 a09 的實際結果填進 R08-4 紀錄，更新總表的 R08-4 與 V-11 列，00 主企劃 a08 到 a10 打勾，本機提交。

### a11 V-11 第一版判定

| 條件 | V-11 第一版 |
| --- | --- |
| R08-2a、R08-3、R08-4 都有紀錄且通過 | pass |
| 任一項 fail | fail，寫明哪一項與原因 |
| 任一項沒做完 | not-run，寫明缺什麼 |

a09 第 2 層沒跑但 a08 的 `full` 通過時，R08-4 仍可判通過，紀錄須寫明 GitHub 上只跑了第 1 層。

## 誰做什麼

| 動作 | 誰 |
| --- | --- |
| a05 建 release、上傳 parquet、定 tag | 擁有者 |
| a06 推送本機提交 | 擁有者 |
| a08、a10、a11 | 程式 session |
| a09 觸發 workflow | 程式 session，先問擁有者 |

## 範圍外

- 不改 `lvr_pipeline/` 與 `data/output/`。`scripts/verify_release.py` 要改時先回報原因。
- 不建 release、不推送、不建分支或 PR。
- 第二版的下載核對是 E 段 e05，工具相同。
