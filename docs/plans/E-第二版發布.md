# E 第二版發布

類別 iii：不論 TGOS 情況都能持續做，發布時機由擁有者定。修訂日期：2026-10-20。現況與擁有者決定以 [00 主企劃](00-主企劃.md)與[擁有者決定與驗收總表](擁有者決定與驗收總表.md)為準，本文件只補這一段的工作、規格與陷阱。

## 目的

發布含 TGOS 回補座標的第二版，並補上第一版漏掉的 380,134 個已定位門牌。第二版用的 run 必須含 B、C、D 三段全部程式修正，所以要全量重跑一次，重做發布用的兩項驗收，再走與第一版相同的發布與下載核對。這個 run 成為新基線。之後每一批回傳與每一筆判定經 D 段的回補補進公開檔案，不再整批重來。

## 發布時機

依 10-18 決定，第二版在新基線後先發，後到批次走回補。實際日期由擁有者在 e01 定，兩種選法：

| 選法 | 內容 | 代價 |
| --- | --- | --- |
| 等 14 批到齊 | 約 10-28 匯入完成，10-29 重跑與驗收，10-30 發布 | 第二版一次含全部首輪回傳，不需第一次回補 |
| 先發 | 任一天以當時已匯入的批次重跑發布 | 之後每批走回補，受影響月份重建 |

## 工作

### E-0 重跑前的程式收尾

這幾項是 B 段延後與 C 段累積的程式修改，都會改離線結果或門牌鍵，必須在 e02 重跑前一次做完。

- [ ] e00 地址池讀帳本中 `tgos_district_fill` 的採用結果補區，原 b13 後半。第 1 批沒有缺區查詢，目前只有第 14 批 4,314 筆缺區查詢會用到
- [ ] e07 offline-state 的 `quality.json` 記錄規則版本，使 R07-6a 護欄能拒絕其他規則版本的離線狀態
- [x] e08 `code_commit` 取值：原本 `lvr_pipeline/storage/runs.py:502` 以 `git -C ROOT rev-parse HEAD` 取值，從 `git archive` 凍結複本執行時會讀到主 repo 當時的 HEAD。10-20 改為 `current_code_commit()`：`git archive` 以 `export-subst` 把提交寫進 `lvr_pipeline/_commit.txt`，凍結複本讀這個檔；git 工作區只在頂層等於 ROOT 時取 HEAD；兩者都給不出時拒絕。凍結複本的 `working_tree_source_dirty` 記為 false
- [ ] e09 C 段 c26 累積的解析修正：村里名重複如 `廣盛村廣盛村`、`斗煥里斗煥里`；眷村或地名被解析成村里如 `石門新村18號`、`民享村17號`。會改門牌鍵，依 R07-6b 方式寫正反案例進地址規則，規則版本升版
- [x] e10 V-10b 第 4 項改為獨立計算：以地址出現鍵算已定位門牌數，再與輸出列數比，不得沿用輸出程式的對應。寫成檢查腳本的一部分。10-20 完成，腳本 `data/tmp/e/e10/check.py`，第一版基線報出應有 3,931,858、輸出 3,551,724、缺 380,134，第 4 項 fail。缺漏的成因以補區門牌為主：`road_only_unique` 263,618、`road_unique_in_county` 104,616，地址池改名只占 10,310，另有 `coordinate_confirmed_candidate` 1,495 與無原鍵 96；集合比對另有 1 列輸出不在離線定位內。e12 用此腳本
- [ ] e11 全套測試在 `git archive HEAD` 乾淨複本跑，紀錄，提交

### E-1 擁有者同意

- [ ] e01 擁有者同意這一次發布並定日期。發布前先告訴擁有者當時已匯入幾批、採用多少

### E-2 全量重跑與驗收

- [ ] e02 全量 `run-full` 帶入帳本：`--tgos-ledger` 指 `data/runs/r09-7b-20261018/tgos-state/` 最新快照，`--review-decisions` 指 `data/review-decisions/`，新 run ID，`--cutoff` 與 DuckDB 上限依總表。階段表 offline-index 到 run-report 含 `tgos-state` 與 `final-state`。程式必含 `7d969c2` 門牌對應修正。帳本中全部已完成批次以當時規則重新匯入，紀錄各批採用數與 C 段逐批匯入時的差異
- [ ] e12 重做 V-10b 九項，第 4 項依 e10。輸出列數應為離線定位加 TGOS 採用的門牌數，寫明與第一版 3,551,724 的差異來源：380,134 漏掉的門牌、TGOS 回補、解析修正造成的增減
- [ ] e13 重做 R08-2a：`scripts/maintenance.py export --run-id <新 run>` 匯出維護包；乾淨 Windows 環境重產 556 檔逐檔相同；Linux 容器重產逐檔相同。Linux 相符才算 B-2 距離精度 b10 通過，也就是 R08-2b 結案
- [ ] e14 新 run 記為新基線，更新 00 與總表的基線、V-10b、R08-2a、R08-2b

### E-3 發布與核對

- [ ] e03 以新基線的 `public/` 與 `release/` 複製到 `data/output/`，逐檔核對 SHA-256、大小、列數等於 catalog 與 `output.json`，`verify_output` 通過。第一版檔案被覆寫，依月份分批提交，每個提交小於 2 GB。變更內容寫在 release 說明草稿：補齊 380,134 列、TGOS 回補筆數、規則版本
- [ ] e04 擁有者建 release、上傳 GeoParquet、推送本機提交，tag 由擁有者定
- [ ] e05 R08-4 第二版：`scripts/verify_release.py --tag <tag> --code-commit <新基線程式提交> --code-python <絕對路徑>` 完整核對，再觸發 `verify-release.yml`。核對用的程式提交是產生第二版的那個，不是當時 HEAD
- [ ] e06 V-11 第二版：R08-2a、R08-3、R08-4 第二版都有紀錄且通過才寫 pass。更新總表與 00

### E-4 發布後的回補節奏

- [ ] e15 之後每批回傳匯入後，以 `run-full --tgos-ledger --review-decisions` 跑新 run，只有 tgos-state、review、final-state、output 重建，受影響月份依 D 段的 `affected_months.json` 判定。何時把回補結果再發布由擁有者定，每次發布都走 e03 到 e06

## 程式事實與行號

以 HEAD `e94e1c3` 查定，施做前先 `grep` 確認。

| 事實 | 位置 |
| --- | --- |
| `run-full` 參數：`--run-id`、`--cutoff`、`--address-dir`、`--runs-root`、`--tgos-ledger`、`--review-decisions` | `lvr_pipeline/cli.py:33-53` |
| `RUN_FULL_STAGES` 含 `tgos-state` 與 `final-state`；`DEFAULT_REVIEW_DECISIONS` | `lvr_pipeline/pipeline.py:330`、`:334` |
| `code_commit` 以 `git -C ROOT rev-parse HEAD` 取值 | `lvr_pipeline/storage/runs.py:502` |
| 維護包匯出與重產 `export_maintenance`、`rebuild_from_maintenance`，CLI `--run-id`、`--package`、`--out` | `scripts/maintenance.py:176`、`:244`、`:348-352` |
| 下載核對參數 `--tag`、`--commit`、`--expected-catalog-sha256`、`--mode`、`--code-commit`、`--code-python`、`--candidate` | `scripts/verify_release.py:437-449` |
| `DISTRICT_FILL_RULE` 與地址池補區綁定 | `lvr_pipeline/addresses/pool.py:40`、`:142`、`:294` |
| 門牌對應修正：輸出與 final-state 經地址出現鍵對應結果 | 提交 `7d969c2`，`lvr_pipeline/output/rows.py`、`lvr_pipeline/results/final_state.py` |

## 陷阱

- 任何 `lvr_pipeline/` 修改都使之前 run 的指紋失效。e00 到 e11 要一次做完再跑 e02，不要每項跑一次。
- e02 之後若 C 段再有規則修改，發布前要再重跑一次。累積到發布前。
- 第一版的 `data/output/` 已在 GitHub，e03 覆寫時 repo 再加約 10 GB，擁有者 10-17 已接受。
- 第二版核對必須用產生第二版的程式提交，`verify_release.py` 已支援 `--code-commit`，`--code-python` 要給絕對路徑。
- 乾淨 Windows 與 Linux 容器重產的做法見 [R08-2a 紀錄](../records/R08-2a維護包重產.md)，Linux 以本機 Docker 的 Ubuntu 容器，只掛載程式與維護包。
- 全套測試在 `git archive HEAD` 乾淨複本跑，提交只用 `git add <明確路徑>`。
