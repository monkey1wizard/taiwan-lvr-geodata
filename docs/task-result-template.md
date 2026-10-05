# Cloud task 執行結果範本

依完整企劃的 task 前置條件與 test points 填寫。執行環境不需要 GAL 或本機 .dev。這份範本沒有宣稱任何 task 已完成。

## Task 與前置條件

| 項目 | 填寫內容 |
| --- | --- |
| Task ID／phase | T-NN／PN |
| 狀態 | Planned／Running／Accepted／Failed／Waiting |
| 程式 repo／commit／PR | 實際版本與可審查變更 |
| 前置 tasks | 每項已驗收交付成果及版本 |
| 執行範圍 | 樣本／指定縣市類別／單季／全清單 |
| 環境 | OS、Python／套件版本、CPU、記憶體、可用磁碟 |
| 相關決策 | 沿用 GitHub 公開與舊 TGOS 規則，記錄此 task 實測資源及必要憑證存取方式，不寫憑證值 |

## 輸入與命令

記錄輸入 manifest URI／SHA-256、檔案雜湊、結構／地址鍵版本、來源 commit、parent snapshot 與設定。只填可取得且已驗證的位置。憑證內容不寫入報告。

逐項填寫已實作的命令、工作目錄、參數及結束代碼。不能把企劃中的預計命令當成現有命令。

## Test points

| Test point | 實際命令或人工檢查 | 結果 | 證據及限制 |
| --- | --- | --- | --- |
| TP-NN | 完整命令／輸入 | pass／fail／not-run | 結果檔案、預期／實際差異、未測範圍 |

若相關 test point 失敗或尚未執行，該 task 尚未驗收。實作程式、樣本測試及真實部署分別記錄，不混稱全部通過。

## Output data 與資源

| 項目 | 填寫內容 |
| --- | --- |
| Snapshot／manifest | ID、parent、SHA-256、schema／producer 版本 |
| 資料粒度 | source_observation 或已證明的交易識別，說明空 transaction_key |
| 範圍與完整性 | 選定／缺失來源、交易月 tx_yyyymm、各類別筆數、已定位／未定位／衝突 |
| 月下載 | YYYYMM_category 檔名、格式、公開 URL、雜湊及 month_coverage_status |
| 年度包 | 年份／格式、包含／缺失／空月份、曆年完整性、原月檔雜湊與分片索引 |
| 更新對應 | 回補影響月份及重建的年度包，未變檔案雜湊與前版 |
| TGOS 狀態 | 初始 tgos_started=false，開始後須有保留／查詢帳本及雜湊 |
| 來源核對 | 原始列的保留／排除／失敗去向，以及成員外部索引鍵 |
| 檔案 | 相對路徑、位元組數、SHA-256、讀回結果 |
| 資源 | 執行時間、RSS 峰值、下載量、暫存／磁碟峰值、剩餘空間 |
| 小型輸出評估 | 月檔／年包／維護狀態大小、暫存峰值、Git 歷史成長及 Git／Release 配置 |
| 交接位置 | 可取得的 GitHub 公開 URL、版本／雜湊及保留紀錄；artifact 記錄到期條件 |

若輸出只存在會消失的工作目錄，就標示「尚未持久交接」。GitHub 公開方向已確定，發布前核對月／年與維護附件完整性，再更新版本索引；上傳失敗保留前版。一般使用者只下載月檔或年包，不要求下載整份維護狀態。憑證不進公開資料。

## 結論與下一個 task

說明已完成的範圍、尚未驗收的 test points、實際阻擋及可接續的 task。指明下一位 agent 如何取得相同輸入及輸出。採用已驗證離線快照時，說明是否需要 raw ZIP 重建，不能省略不相容檢查。

## 本次 P2 執行結果：2026-10-05

T-08～T-13、T-22、T-23 已在宣告的初版範圍驗收。正式工作目錄為 `C:/Code/taiwan-lvr-geodata`，所有變更在 main 提交並直接推送。資料 producer 為 `793fef98f84cab5cc9fc3126af9cd76e5d480bdb`，發布修正版本為 `00691a8955a2e98d73b6f04a97289b9d30a7c965`，指標提交為 `c7d64cedbf6b1ed69431d3a20ae885b67ea0d934`。較早的索引／池／狀態與 P1 producer 版本及輸入 manifest 雜湊見 [p2-evidence.json](p2-evidence.json)。

| 項目 | 實際結果 |
| --- | --- |
| 前置條件 | P0／P1 T-01～T-07 已驗收，使用 p1-115q1-accepted |
| 輸入範圍 | 115q1，102,743 筆來源觀測，102,881 個地址成員 |
| 地址來源 | 固定地址 repo 的行政區參考，座標使用固定官方臺北市 CSV，沒有修改地址 repo |
| 公開版本 | p2-115q1-offline-v2，不可變 Release，914 個附件下載核對 |
| 月／年 | 87 交易月、783 個月檔、10 年／30 年度 ZIP，均為 scope_limited |
| 定位 | 9,946 筆有幾何、92,797 筆 null，另保留全部來源與未定位狀態 |
| 維護交接 | 約 77.6 MiB，cloud 不提供 raw 重產全部月檔雜湊相同 |
| 完整資料與資源 | 詳見 [P2 紀錄](p2-offline-output.md)，生成檔案不進 Git |

以下命令皆從正式 repo 根目錄執行。資料建置已完成，重跑須使用未占用的 run ID。重用快照時，producer、結構版本、設定與輸入雜湊須全部相同。表中是實際使用的命令，不依賴 GAL。

| 命令 | 結果 | 證據／限制 |
| --- | --- | --- |
| `python -m pytest -q` | pass，211 tests | Windows 124.13 秒，Linux 60.37 秒；測試全部使用合成輸入 |
| `python -m lvr_pipeline build-offline-index --address-dir ../taiwan-address-data --county 63 --official-source data/sources/official_taipei.json --official-file data/cache/official-source/taipei-address.csv --run-id p2-taipei-official-v2` | pass | 1,157,763 列，來源 SHA 及行政區已核對 |
| `python -m lvr_pipeline build-address-pool --input data/work/converted/snapshots/p1-115q1-accepted --address-dir ../taiwan-address-data --index data/work/offline-index/snapshots/p2-taipei-official-v2 --run-id p2-115q1-pool` | pass | 56,001 唯一鍵，102,881 成員關聯 |
| `python -m lvr_pipeline resolve-offline --pool data/work/address-pool/snapshots/p2-115q1-pool --index data/work/offline-index/snapshots/p2-taipei-official-v2 --run-id p2-115q1-state` | pass | located 6,481、conflict 26、unmatched 91、outside_scope 49,403；不任取第一列 |
| `python -m lvr_pipeline package-output --input data/work/converted/snapshots/p1-115q1-accepted --state data/work/offline-state/snapshots/p2-115q1-state --notices data/sources/p2_notice.json --run-id p2-115q1-offline-v2` | pass | 完成前後均核對三格式、年度原月檔及維護狀態 |
| `python -m lvr_pipeline publish-output --input data/output/p2-115q1-offline --expected-parent 462a39a37ea638d5f5ef6c616dc9865a087683b4 --checkout . --receipt data/work/p2-release-receipt.json` | fail | 零位元組空 NDJSON 遭 GitHub 拒絕，沒有提交指標 |
| `python -m lvr_pipeline publish-output --input data/output/p2-115q1-offline-v2 --expected-parent 793fef98f84cab5cc9fc3126af9cd76e5d480bdb --checkout . --receipt data/work/p2-release-receipt.json` | fail | draft 依 tag 查詢 404，零附件，沒有提交指標 |
| `python -m lvr_pipeline publish-output --input data/output/p2-115q1-offline-v2 --expected-parent 00691a8955a2e98d73b6f04a97289b9d30a7c965 --checkout . --receipt data/work/p2-release-receipt.json` | pass | 相同資料 producer，改用 Release ID 接續，914 附件下載雜湊核對後公開 |
| `python -m scripts.verify_public_snapshot --manifest-url https://github.com/monkey1wizard/taiwan-lvr-geodata/releases/download/data-p2-115q1-offline-v2/manifest.json --manifest-sha256 2ac2913b70784e657e6b845c3a558236fddcc73283bcbdd3c6aa61c053a8e028` | pass | 全新 GitHub Ubuntu，raw_provided=false，783 個重產月檔全部相同 |
| `python -m lvr_pipeline commit-release-pointer --receipt data/work/p2-release-receipt.json --checkout .` | pass | Linux 交接通過後提交／推送 c7d64ce，預期 parent 及公開不可變 digest 再次核對 |
| P3 TGOS 準備／匯入／回補 | not-run | 此階段沒有開始 TGOS，tgos_started=false |
| P4 地址 repo patch／更新 | not-run | 此階段沒有修改或發布相關地址 repo |
| P5 全歷史量測 | not-run | 不以單批 scope_limited 宣稱全歷史驗收 |

TP-08～TP-14、TP-23～TP-26 的 P2 適用驗收為 pass。本機中斷與 parent 復原由 TP-10 測試核對。TP-09 的 TGOS 整合仍為 not-run。TP-26 的回補更新仍為 not-run。P5 全量驗收也尚未執行。

下一階段為 P3 的 T-14～T-17。從 [公開指標](../data/releases/latest.json)取得固定 URL／SHA-256，再依 [cloud 操作](cloud-runbook.md)下載維護包。持久維護資料在不可變 Release，Actions artifact 只供補充驗證證據。

## 本次 P3 執行結果：2026-10-05

T-14 的持久 TGOS 狀態與配額規則已通過合成整合驗收。T-15 的第一個真實批次已準備完成，但人工上傳與提交確認尚未執行。T-16～T-17 的命令已實作並通過合成測試，但真實回傳匯入與公開回補尚未執行。因此 T-15～T-17 保持未勾選。完整機器可讀數字見 [p3-evidence.json](p3-evidence.json)。

| 項目 | 實際結果 |
| --- | --- |
| 地址 repo 更新 | `C:/Code/taiwan-address-data` 從 `752c87d…` fast-forward 到乾淨的 `02887978…` |
| 固定地址來源 | 27,176 個 road CSV，共 1,575,111,184 bytes；再散布權利為 `pending_upstream_evidence` |
| 完整離線索引 | 10,624,627 列；9,491,547 列有效；1,133,080 列無效但保留；22 個縣市代碼 |
| 115q1 地址池 | 56,022 個唯一鍵、102,881 個地址成員 |
| 最新離線狀態 | located 48,882、conflict 2,269、unmatched 4,871、outside_scope 0 |
| TGOS 候選 | 4,871；conflict 不自動送查，沒有為湊滿 10,000 建立資料列 |
| 真實 TGOS 批次 | prepared；`p3-tgos-20261005-001`，服務日期 2026-10-05，`external_used=0`，4,871 筆唯一地址 |
| 真實匯入／回補 | not-run；需先完成真實批次、人工上傳及回傳下載 |
| 公開發布 | not-run；目前地址來源權利未完成，且尚無 TGOS 回傳可供回補 |

以下命令在正式 repo 根目錄執行。實際 snapshot 路徑位於忽略的 `data/work/`，未提交到 Git。

| 命令 | 結果 | 證據／限制 |
| --- | --- | --- |
| `git -C C:/Code/taiwan-address-data pull --ff-only` | pass | fast-forward 到 `02887978ef19c1067e339787bae976a72d4723af`，工作樹乾淨 |
| `python -m lvr_pipeline pin-address-source --address-dir C:/Code/taiwan-address-data --output data/sources/address_source.json` | pass | 27,176 個 road CSV；描述檔已更新 |
| `python -m lvr_pipeline build-offline-index … --run-id p3-address-02887978-clean` | pass | 599,884,131 bytes；381.85 秒；RSS 峰值 602,394,624 bytes；producer 工作樹乾淨 |
| `python -m lvr_pipeline build-address-pool … --run-id p3-115q1-pool-02887978-clean` | pass | 56,022 個地址鍵、102,881 個成員；19.75 秒；producer 工作樹乾淨 |
| `python -m lvr_pipeline resolve-offline … --run-id p3-115q1-offline-02887978-clean` | pass | located 48,882、conflict 2,269、unmatched 4,871；44.22 秒；producer 工作樹乾淨 |
| `python -m pytest -q` | pass，219 tests | Windows 178.10 秒；GitHub Ubuntu 53.18 秒；全部測試使用合成輸入 |
| `prepare-tgos … --service-date 2026-10-05 --external-used 0 --run-id p3-tgos-20261005-001` | fail | Windows sandbox 無法存取暫存目錄，未提交快照或交付檔案 |
| `prepare-tgos … --service-date 2026-10-05 --external-used 0 --run-id p3-tgos-20261005-001` | fail | 產生的檔案只有 `Address` 欄。TGOS 拒絕上傳，因為標頭未遵守既有五欄契約。未送出任何查詢。 |
| `repair-tgos-exchange --state data/work/tgos-state/snapshots/p3-tgos-20261005-001-format-fix --batch p3-tgos-20261005-001 --run-id p3-tgos-20261005-001-format-fix-clean` | pass | 4,871 筆，標頭固定為 `id,Address,Response_Address,Response_X,Response_Y`，後三欄留空，UTF-8-sig；批次仍為 `prepared`，沒有新增配額保留 |
| `verify-tgos-state --state data/work/tgos-state/snapshots/p3-tgos-20261005-001-format-fix-clean` | pass | 批次狀態為 `prepared`，批次數 1；located 48,882、conflict 2,269、unmatched 4,871 |
| `import-tgos` 真實回傳 | not-run | 尚無人工下載的真實回傳檔 |
| `backfill-output` 真實輸出 | not-run | 合成測試證明只改受影響月份並保留前版；真實匯入是前置條件 |

TP-15～TP-18 的合成情境包含 10,001 候選、公平選取、未來日期／額度耗盡、提交前取消、提交不明、UTF-8-sig、一對一 Address、重複匯入、座標軸疑似顛倒及單月回補。T-14 已具備持久狀態證據。T-15 已完成真實批次準備，但尚未完成人工上傳與提交確認。T-16 的真實回傳仍為 not-run。T-17 的真實回補仍為 not-run。

下一步由操作員將 `data/tgos/p3-tgos-20261005-001/addresses.csv` 上傳 addrCompare。manifest 只供核對，不上傳。座標系選擇 WGS84 經緯度。在「模糊比對規則設定」區塊開啟「分單／雙號比對」，誤差選擇「不限」，回傳筆數選擇「僅回傳一筆」，其餘選項不勾選。操作員須回報提交成功、提交狀態不明或上傳前取消，系統才能更新持久狀態。TGOS 完成後，下載真實回傳檔並執行 T-16。
