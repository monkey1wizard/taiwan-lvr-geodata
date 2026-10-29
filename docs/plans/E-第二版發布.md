# E 第二版發布

類別 iii：不論 TGOS 情況都能持續做，發布時機由擁有者定。修訂日期：2026-10-21。現況與擁有者決定以 [00 主企劃](00-主企劃.md)與[擁有者決定與驗收總表](擁有者決定與驗收總表.md)為準，本文件只補這一段的工作、規格與陷阱。

## 目的

發布含 TGOS 回補座標的第二版，並補上第一版漏掉的 380,134 個已定位門牌。第二版用的 run 必須含 B、C、D 三段全部程式修正，所以要全量重跑一次，重做發布用的兩項驗收，再走與第一版相同的發布與下載核對。這個 run 成為新基線。之後每一批回傳與每一筆判定經 D 段的回補補進公開檔案，不再整批重來。

## 重跑與發布是兩個決定

- **重跑**（e02 到 e14）：在本機全量 `run-full`，產生新基線並驗收。不動 `data/output/`，不上傳 GitHub，公開檔案維持第一版。
- **發布**（e16、e03 到 e06）：把新基線的輸出覆寫到 `data/output/`，擁有者建 release 並推送。發布前必須已經重跑且驗收通過。

兩者分開決定。依 10-18 決定，第二版在新基線後先發，後到批次走回補。

### 重跑時機（e01）

| 選法 | 內容 | 代價 |
| --- | --- | --- |
| TGOS 做到一半就重跑 | 任一天以當時帳本最新快照重跑。已匯入批次以現行規則重新匯入；尚未送出或尚未回傳的批次，門牌在這次 run 仍是 unmatched | 後到批次每批走 e15 回補，約 50 分鐘；第 14 批是缺區查詢，依 e00 會使地址池之後全部重建，接近 3 小時。之後 C 段若再改採用規則，發布前要再重跑 |
| 等 14 批到齊 | 約 10-28 匯入完，10-29 重跑與驗收 | 中間不能利用新基線 |

TGOS 做到一半就重跑的好處：一是 `v2.9` 可能使約 1,300 個地名型門牌離線就定位，尚未送出的批次是從 `v2.8` 過渡基線產生的，可以對新基線檢查哪些查詢已不必送，取消與重產由 C 段提案、擁有者裁定；二是以 `v2.9` 程式在 `v2.8` 離線狀態上重建帳本快照會驗證失敗，新基線的離線狀態是 `v2.9`，C 段之後不再遇到。每個 run 約占 32 GB。

### 發布時機（e16）

| 選法 | 內容 | 代價 |
| --- | --- | --- |
| 重跑驗收後就發 | e12、e13 通過即走 e03 | 第二版只含當時已匯入的回傳，其餘之後再發一版 |
| 等回補後再發 | 後到批次走完 e15 再發 | 380,134 列的補齊也跟著延後 |

## 工作

### E-0 重跑前的程式收尾

這幾項是 B 段延後與 C 段累積的程式修改，都會改離線結果或門牌鍵，必須在 e02 重跑前一次做完。

- [x] e00 地址池讀帳本中 `tgos_district_fill` 的採用結果補區，原 b13 後半。第 1 批沒有缺區查詢，目前只有第 14 批 4,314 筆缺區查詢會用到。10-20 完成：`run-full --tgos-ledger` 把帳本交給地址池，地址池以現行規則判定缺區回傳，採用時以交易文字補上回傳的區重算鍵，村里與鄰不取自 TGOS，原因記 `tgos_district_fill`；綁定只加採用清單摘要；`tgos-state` 以查詢指紋把回傳座標給補區鍵。規則見[地址規則](../地址規則.md)缺區補區第 9 至 12 條。缺區回傳的證據目前在 `tgos/batches.py` 的 `_write_merged_state` 補上，因 `validate.py` 由 C 段維護，C 段可改放 `apply_response`
- [x] e07 offline-state 的 `quality.json` 記錄規則版本，使 R07-6a 護欄能拒絕其他規則版本的離線狀態。10-20 完成：`lvr_pipeline/results/reconcile.py` 綁定參數加 `normalization_version`；`load_p2`、`prepare-tgos` 與 `import-tgos` 讀到不同版本時拒絕，未記錄版本的舊狀態照舊可讀；`run-full --tgos-ledger` 帶入帳本仍不判規則版本
- [x] e08 `code_commit` 取值：原本 `lvr_pipeline/storage/runs.py:502` 以 `git -C ROOT rev-parse HEAD` 取值，從 `git archive` 凍結複本執行時會讀到主 repo 當時的 HEAD。10-20 改為 `current_code_commit()`：`git archive` 以 `export-subst` 把提交寫進 `lvr_pipeline/_commit.txt`，凍結複本讀這個檔；git 工作區只在頂層等於 ROOT 時取 HEAD；兩者都給不出時拒絕。凍結複本的 `working_tree_source_dirty` 記為 false
- [x] e09 C 段 c26 累積的解析修正：村里名重複如 `廣盛村廣盛村`、`斗煥里斗煥里`；眷村或地名被解析成村里如 `石門新村18號`、`民享村17號`。會改門牌鍵，依 R07-6b 方式寫正反案例進地址規則，規則版本升版。10-20 完成，與 C 段採用第 8 條一起提交為 `717ec29`，`v2.9`。量測（過渡基線 `b16-20261018b`）：村里名重複 14 鍵；不在村里表的村里接門牌 1,104 鍵，全部 unmatched，其中以街路等於該名稱對地址基底粗估單一座標 1,317 個（較寬條件）。案例見[地址規則](../地址規則.md)「村里名重複與地名」
- [x] e10 V-10b 第 4 項改為獨立計算：以地址出現鍵算已定位門牌數，再與輸出列數比，不得沿用輸出程式的對應。寫成檢查腳本的一部分。10-20 完成，腳本 `data/tmp/e/e10/check.py`，第一版基線報出應有 3,931,858、輸出 3,551,724、缺 380,134，第 4 項 fail。缺漏的成因以補區門牌為主：`road_only_unique` 263,618、`road_unique_in_county` 104,616，地址池改名只占 10,310，另有 `coordinate_confirmed_candidate` 1,495 與無原鍵 96；集合比對另有 1 列輸出不在離線定位內：`106q3/e_lvr_land_a.csv/27`，地址 `高雄市新興區民主路38號（曾?號）`，地址池記 `candidate_review` 不給鍵，舊輸出以成員原鍵寫出，是第一版的真實錯誤，`7d969c2` 已不再寫出。e12 用此腳本
- [x] e11 全套測試在 `git archive HEAD` 乾淨複本跑，紀錄，提交。10-20 完成，1,103 passed、3 個已知環境失敗在 repo 內通過，見 [E-0 紀錄](../records/E-0重跑前程式收尾.md)

### E-1 擁有者同意重跑

- [x] e01 擁有者同意執行 e02 全量重跑，並選重跑時機，見上方「重跑時機」。Claude 先報告：已匯入幾批與各批採用數、尚未送出與尚未回傳的批次數、磁碟餘裕、預估耗時（全量約 3 小時，加 e12、e13 驗收）。擁有者回答「現在跑」或「第 N 批匯入後跑」。這一步只同意重跑，不同意發布。10-26 擁有者回答「開始執行 E」，以已完成的 10 批立即重跑

### E-2 全量重跑與驗收

- [x] e02 全量 `run-full` 帶入帳本：`--tgos-ledger` 指 `data/runs/r09-7b-20261018/tgos-state/` 最新快照，`--review-decisions` 指 `data/review-decisions/`，新 run ID，`--cutoff` 與 DuckDB 上限依總表。階段表 offline-index 到 run-report 含 `tgos-state` 與 `final-state`。程式必含 `7d969c2` 門牌對應修正。帳本中全部已完成批次以當時規則重新匯入，紀錄各批採用數與 C 段逐批匯入時的差異。10-26 完成，run `e02-20261026`，4,051,555 列，TGOS 採用 66,164，見[E-2 紀錄](../records/E-2第二版基線.md)
- [x] e12 重做 V-10b 九項，第 4 項依 e10，集合比對兩方向都要為 0，並核對輸出不含地址池沒有給鍵的門牌。輸出列數應為離線定位加 TGOS 採用的門牌數，寫明與第一版 3,551,724 的差異來源：380,134 漏掉的門牌、TGOS 回補、解析修正造成的增減。10-26 執行：8 項 pass，第 5 項 not-run，缺階段內磁碟取樣與 run 目錄最大值，見[E-2 紀錄](../records/E-2第二版基線.md)。10-27 以 `e02m-20261026` 帶取樣重跑量得 run 目錄最大 41,529,249,405 bytes，磁碟最低 19.62%，低於 20% 的部分是 run 目錄以外的寫入。擁有者決定第 5 項以 run 目錄最大值推算記為 pass，V-10b 通過
- [x] e13 重做 R08-2a：`scripts/maintenance.py export --run-id <新 run>` 匯出維護包；乾淨 Windows 環境重產 556 檔逐檔相同；Linux 容器重產逐檔相同。Linux 相符才算 B-2 距離精度 b10 通過，也就是 R08-2b 結案。10-26 通過：兩環境都是 557 檔相同，含 `catalog.json`。b10 通過，R08-2b 結案
- [x] e14 新 run 記為新基線，更新 00 與總表的基線、V-10b、R08-2a、R08-2b。10-26 `e02-20261026` 記為第二版基線候選，10-27 V-10b 通過後確定為第二版基線。10-29 含 `v2.11` 與 c53 的 `e17b-20261028` 重做 e02、e12、e13 全部通過，取代為第二版基線，見[e17 紀錄](../records/E-2第二版重跑e17.md)

### E-3 發布與核對

- [x] e16 擁有者同意發布第二版並定日期，見上方「發布時機」。前提是 e12、e13 通過。Claude 先報告：新基線列數、與第一版的差異（補齊的門牌、TGOS 回補筆數、解析修正的增減）、尚未回傳的批次。擁有者回答「發布」並給日期，或「等第 N 批回補後再發」。同意後才做 e03。10-29 擁有者同意以 `e17b-20261028` 發第二版，c54 留到第三版，日期未另定，見[總表](擁有者決定與驗收總表.md)決定表
- [x] e03 以新基線的 `public/` 與 `release/` 複製到 `data/output/`，逐檔核對 SHA-256、大小、列數等於 catalog 與 `output.json`，`verify_output` 通過。第一版檔案被覆寫，依月份分批提交，每個提交小於 2 GB。變更內容寫在 release 說明草稿：補齊 380,134 列、TGOS 回補筆數、規則版本。10-29 完成：`e17b-20261028` 的 556 檔覆寫到 `data/output/`，逐檔核對與 `verify_output` 8 項通過，4,076,309 列。依月份 21 個提交加 catalog 提交 `74745c0`，見[E-3 紀錄](../records/E-3第二版發布.md)
- [x] e04 擁有者建 release、上傳 GeoParquet、推送本機提交，tag 由擁有者定。10-29 擁有者要求 Claude 以 CLI 執行：推送到 `ad5a44d`，release `v20261029`，附件雜湊相符，見[E-3 紀錄](../records/E-3第二版發布.md)
- [x] e05 R08-4 第二版：`scripts/verify_release.py --tag <tag> --code-commit <新基線程式提交> --code-python <絕對路徑>` 完整核對，再觸發 `verify-release.yml`。核對用的程式提交是產生第二版的那個，不是當時 HEAD。10-29 本機完整核對 10 項 pass、`complete=true`，資料提交需指定 `--commit ad5a44d…`，因為預設值仍是第一版的 `b21217e`。GitHub 核對同日由擁有者要求觸發，run `40810516316` success，兩層都通過，見[R08-4 第二版紀錄](../records/R08-4第二版下載核對.md)
- [x] e06 V-11 第二版：R08-2a、R08-3、R08-4 第二版都有紀錄且通過才寫 pass。更新總表與 00。10-29 R08-2a、R08-3 即 e03、R08-4 本機與 GitHub 兩層都通過，V-11 第二版 pass

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
