# 台灣實價登錄地理資料與地址回補完整企劃

> 修訂日期：2026-10-05。本文件是 cloud agent 的完整執行企劃，包含六個 phases、24 個 tasks 與 26 個 test points。P1 建立內部轉換階段快照；P2 完成離線定位後，先交付公開月 output、年度包及可接續維護狀態；P3 再做 TGOS 增補。
>
> 月份依交易／租賃日期 tx_yyyymm 劃分，保留 YYYYMM_category 檔名。使用者可獨立下載月／類別／格式，也可選年度包。GitHub 公開及舊 TGOS 操作條件已確定，不再保留原三個 OQ。
>
> P0／P1 的 T-01～T-07、P2 的 T-08～T-13、T-22、T-23，以及 P3 的 T-14 已驗收。P3 完整測試在 Windows 為 218 個通過；本次 Linux 驗證待推送後由 GitHub Actions 執行。115q1 首版公開離線 output 已保存於不可變 GitHub Release。新版地址來源已完成真實離線重建，留下 4,871 個 TGOS 候選；真實 TGOS 批次仍等待操作員確認共用帳號當日外部用量。見 [P0](../p0-foundations.md)、[P1](../p1-conversion.md)、[P2 驗收](../p2-offline-output.md)與 [P3 執行證據](../p3-evidence.json)。所有程式在本機正式目錄 main commit／push，cloud 不需要 GAL、`.dev` 或另產生提示。

## 審核狀態

- 使用者已確定 GitHub 公開、交易月最小下載單位及年度包、離線處理後首版 output，以及沿用舊 TGOS 條件。
- 修訂設計已通過獨立架構審查。
- 未請求介面設計及商業審查。
- 各 task 依自己的前置條件與 test points 驗收，不要求完整 GAL。

## 目標與範圍

P0～P2 已完成本階段驗收。P2 交付 115q1 的 102,743 筆來源觀測、87 個交易月份、10 個年度及可接續維護狀態。P3 的持久配額／查詢狀態已驗收，TGOS 準備、嚴格匯入、別名事件與回補程式已通過合成整合；真實人工交換尚未執行。詳見 [P1 紀錄](../p1-conversion.md)、[P2 紀錄](../p2-offline-output.md)與 [P3 結果](../task-result-template.md#本次-p3-執行結果2026-10-05)。

以 taiwan-lvr-geodata 建立可重建的台灣實價登錄地理資料管線。taiwan-address-data 提供固定版本的離線門牌座標，並接收經驗證的新地址資料。雲端 agent 必須能取得相同輸入、接續人工 TGOS 輪次、回補各年份與三種交易類別，再發布有版本紀錄的 GIS 資料。

P0～P2 已驗收。P3 的 T-14 已驗收；T-15～T-17 程式與合成整合已完成，真實批次、回傳與公開回補仍按實際證據驗收。P4～P5 尚待完成。使用者要求 GitHub 公開交付、TGOS 前先交付離線處理 output、以交易月為最小下載單位並提供年度包，以及 cloud agent 可直接使用的 phases／tasks／test points，不要求完整 GAL 流程。沿用草案資料格式與舊 TGOS 操作條件。原 Google Drive 草稿與 legacy 文件保留為參考快照。

## 已確認現況

以下為 2026-10-04、Asia/Taipei 的規劃基準盤點。P0～P2 的後續實作與驗收另見階段紀錄，不能將基準表視為目前分支尚未實作的宣告。現有程式碼優先於過時圖譜與說明文件。

| 項目 | 已確認現況 | 規劃影響 |
| --- | --- | --- |
| 主專案 | C:/Code/taiwan-lvr-geodata；公開 repo monkey1wizard/taiwan-lvr-geodata；main 為 84542193d02f48dce43a6be0e86bcfdd55269aa8 | 28 個起始檔案，搬移時 94 個測試通過，新管線尚未實作 |
| 地址專案 | C:/Code/taiwan-address-data；公開 repo monkey1wizard/taiwan-address-data；HEAD 為 752c87d36a8e52d9b71680115c1c19d1a6d3e4ec | 開發時鎖定這個基準，正式每輪執行另記錄選定 commit |
| 原始 ZIP | 舊專案有 58 份有效 ZIP，約 642.94 MiB，批次從 101q1 到 115q2 | 新專案尚無原始輸入，需完整來源清單與雲端取得方式 |
| 舊工作 CSV | 約 2319.46 MiB | 這是磁碟占用，不能當成記憶體需求，不直接匯入 |
| 地址 CSV | 27,175 個檔案，約 1501.9 MiB，檔名涵蓋 22 個縣市代碼 | 有檔案不代表資料完整或新鮮 |
| 101q1 | ZIP 只有 manifest.csv 與 build.ttt，沒有交易 CSV | 明確標示為已知空批次，不能只檢查 ZIP 格式 |
| 地址更新器 | run() 將縣市資料讀入記憶體，刪除舊路檔再重寫，最後重建 road.csv | 回補需獨立儲存，替換途中中斷可能留下不完整檔案 |
| 來源授權 | 地址 README 宣稱 BSD，未追蹤授權檔，GitHub licenseInfo 為 null | 保存來源條件與顯名紀錄，不以 repo README 取代上游證據 |

盤點時保留的程式只使用 Python 標準函式庫。舊 0_parse_raw／1_normalize 仍維持 CSV 相容行為，舊 parse 的 main() 仍會將 ZIP 成員讀入清單。P1 新命令改用逐列／分批 Arrow 與 DuckDB 驗證。P0／P1 已驗證樣本、Linux 環境與指定單季，但尚未證明雲端全歷史流程可執行。地址 README 宣稱更新器支援 17 縣市，檔名則有 22 個縣市代碼，兩者不能視為相同涵蓋範圍。

## 需求

- [x] 離線定位後、TGOS 完成前交付首版已驗證月 GIS，明確標示粒度／範圍、未定位資料與可接續狀態。
- [x] 提供交易月／類別／格式獨立檔案，以及由相同月檔組成的年度包。
- [ ] 全新 cloud 環境可取得並驗證公開離線快照，支援的 TGOS／回補／輸出動作不需 raw ZIP。
- [ ] 追蹤企劃完整提供 phases、可執行 tasks、前置條件、交付成果與 test points，GAL 為選用。
- [ ] 乾淨 Linux 環境能取得固定版本輸入並驗證雜湊，不需要 Windows 絕對路徑。
- [ ] 全歷史 sales、presale、rent 共用有版本的地址鍵與全域地址池。
- [ ] 首次重建不匯入 SQLite、舊 output 或 migration 座標。後續只接續本管線已驗證的快照。
- [x] 正式資料以 Parquet 保存，DuckDB 在受控記憶體與暫存空間內查詢，不要求長期保存 .duckdb。
- [ ] 每個成功定位結果可回補全部適用的歷史交易，衝突保留供審查。
- [ ] 每輪人工 addrCompare 沿用每日 10,000 筆及每片最多 10,000 行的既有規則，前輪匯入及回補後才產生下一輪。
- [ ] TGOS 失敗是可追溯的查詢紀錄，不是永久排除。允許明確重試與經驗證的新地址表示。
- [ ] 回補 taiwan-address-data 的資料不會在縣市更新時消失，並保持 CSV 與前端介面相容。
- [x] GeoParquet、GeoJSON、NDJSON 使用同一組核准記錄與幾何規則。
- [x] 每個公開版本可追溯輸入、來源版本、決策、輸出雜湊與品質報告。
- [x] 資料及可接續狀態以 GitHub 公開交付，憑證不進發布資料、Git 提交或日誌。
- [x] 中斷或並行執行不會讓讀者取得只完成一部分的快照。

## 流程與專案分工

完整分支與交接步驟見[完整資料處理流程](../data-processing-flow.md)。該文件包含九個子流程，對應本企劃的 tasks 與 test points。P0～P2 及 P3 持久狀態已驗收；TGOS 真實交換／回補與地址更新仍依實際證據完成。

### 專案責任與回補流程

```text
固定 commit 的 taiwan-address-data
  │ 官方 roads + 經驗證補充地址
  ▼
taiwan-lvr-geodata
  │ raw ZIP → Parse/Normalize → 內部型別化階段快照
  │ 正規化成員 → 全域地址池
  ▼
離線精確查詢 → 衝突檢查
  ├──▶ 第一版 OUTPUT：月檔＋年度包＋可接續狀態
  │         → GitHub 公開 → 不重跑 raw 的 cloud 使用端
  └──▶ 未定位地址池 → 一份 TGOS 批次
  │                              ~~▶ 人工上傳與下載
  ◀────────────── 驗證後匯入結果 ──────────┘
  │ 更新地址結果、已驗證別名、全歷史回補
  ├──▶ GIS 快照與品質報告
  └──▶ 來源證據 patch → 地址專案審查／PR
                         → 下輪明確選取新版本，不循環觸發
```

### 單一地址的處理

```text
(start) 正規化地址與 key_version
  ▼
{ 地址鍵有效? }
  ├── 否 ──▶ (end >>|) 診斷或人工覆核，不默默丟棄
  └── 是 ──▶ { 有無歧義的精確結果或已驗證別名? }
                ├── 是 ──▶ (end √) 套用座標並附證據
                └── 否 ──▶ { 座標或來源衝突? }
                              ├── 是 ──▶ (end >>|) 衝突覆核
                              └── 否 ──▶ { 相同查詢已送過? }
                                            ├── 是 ──▶ (end >>|) 等待新證據
                                            │           或明確重試
                                            └── 否 ──▶ (end >>|) 下輪 TGOS 候選
```

## 完整處理方案

### 1. 範圍與專案責任

taiwan-lvr-geodata 負責實價登錄輸入、交易正規化、地址鍵、歷史定位、TGOS 交換、持久狀態、回補與 GIS 輸出。taiwan-address-data 負責官方縣市更新、補充地址保存、roads CSV、road.csv 與 address.js 相容性。首版不新增第三個執行服務、分散式排程器、共用 Python 套件或前端改版。

主專案 coding agent 在 C:/Code/taiwan-lvr-geodata 的 main 實作、測試、commit 並直接 push 至 origin/main，不另建階段分支或 PR。資料工作在不同環境使用同一組命令。人工操作員上傳及下載 TGOS 批次，並在自動證據不足時確認地址別名。線上資料庫載入列為後續選用功能，不納入首版驗收。

### 2. 輸入清單與可重建性

為全部 58 份 ZIP 建立 raw_manifest.json，記錄批次、官方來源、不可變快照位置、位元組大小、SHA-256、取得時間、類別及縣市成員涵蓋、來源條件與已知空批次理由。不存在的批次不能當成空批次。驗證 ZIP 完整性、CSV 標頭、英文說明列與實際交易檔案，未知欄位保留原值並產生結構變更診斷。

地址來源取得完整固定 commit，不下載完整 Git 歷史。另保存路檔雜湊、行政區層與來源證據。COUNTY、TOWN 一律是字串，保留 09007、09020 的前導零。讀取 ROAD 與縣市欄位，不只依清理後的檔名推論位置。快取鍵包含程式 commit、輸入雜湊、正規化與資料結構版本。另行確認搬移前，原始資料仍留在舊專案。

### 3. 執行環境與資源規劃

建議 Python 3.13、DuckDB、PyArrow，測試使用 pytest。只有需要座標系轉換時才使用 pyproj。相容性試作完成後鎖定套件版本。沒有實測需求前不加入 pandas、geopandas 或永久工作資料庫。用 Arrow 分批寫入與標準函式庫逐列讀取 ZIP／CSV，先改掉整份成員及整類資料讀入清單的方式。

驗證分成小型樣本、單季、全歷史三層。每次記錄時間、記憶體峰值、下載量、可用磁碟、暫存量與輸出大小。初期量測建議 2 執行緒、DuckDB 4 GiB 緩衝上限及 4 GiB 暫存上限，仍須符合實際 runner 資源。這是待量測設定，不是效能保證。Python 與 Arrow 額外記憶體另外量測。執行前依實測檢查輸入、輸出、暫存及待提交檔案，保留 20% 餘裕，不足就停止。

| 方案 | 優點 | 成本與限制 | 企劃定位 |
| --- | --- | --- | --- |
| agent 雲端工作 | 修改與測試方便，使用同一命令 | 需確認供應商網路、時間、記憶體與保存能力 | 候選方案 |
| GitHub 標準 Linux runner | 可重複工作流程與報告 | 公開 repo 參考規格為 4 CPU、16 GB RAM、14 GB SSD，單一 hosted job 最長 6 小時；可用磁碟低於標稱容量 | 初始技術量測路徑，由 agent 判斷是否足夠 |
| 較大或自管雲端 runner | 磁碟與執行時間較可控制 | 額外費用、維護與預算決定 | 標準環境實測不足時再評估 |
| 本機 | 除錯與參考核對 | 不能成為雲端自主工作的必要條件 | 選用 |

GitHub 公開交付已確定。先量測可用 cloud／GitHub Actions 資源，必要時按批次處理與交接階段快照。目前沒有實測證明 raw 不能在 GitHub Actions 執行。只有實測需要付費升級時，才另提出具體成本決策。

### 4. 正規化交易資料契約

不可變輸入依 src_batch/category 分割區，另建立正規化層。raw_record_id 由 input_sha256、member_path、source_row_number 計算雜湊。component_id 加上多門牌展開序號。source_serial 與舊複合 ID 保留作為來源證據，不直接當成通用交易識別。

各 category／縣市／來源結構明訂有版本的識別規則。相同編號只是候選，不證明是修訂。若來源字典與全歷史衝突檢查證明識別範圍有效，才可在該範圍定義 transaction_key。通過前保留來源局部鍵，可能重複只報告不合併。修訂需相同已證明識別；原交易日期或案件範圍等固定欄位衝突時，只有明確修正證據才能解釋。再依來源發布或修正證據建立快照／修訂順序，記入來源清單，另存本機 acquired_at。不依下載、交易日期或檔名順序推定。若正式修訂順序未知或同順位，就保留衝突與來源局部觀測，不默默選一筆。缺編號或編號衝突時保留來源局部識別與診斷，不用相似交易自動合併。

核心欄位為 category、src_batch、source_serial、raw_record_id、component_id、transaction_key、group_key、raw_address、normalized_address、building_key、key_version、tx_date_raw、tx_yyyymm、first_observed_snapshot、金額面積欄位、props_json、parse_status。日期驗證固定記錄 run_cutoff_yyyymm，避免執行日期改變結果。金額用整數最小單位或固定小數，區分零與缺值，保存平方公尺與明確單價換算。土地、純車位、無門牌、日期異常另存分類結果並核對筆數，不默默捨棄。

### 5. 全域地址識別與定位

既有 building_key() 函式輸出只存為 legacy_building_key 舊版相容欄位。它會截斷「號」後子門牌並移除「里」，不能視為已證明唯一。新快照採用 key_version=v2。正式 building_key 欄位是新的 v2 結構鍵，地址池、索引、查詢、別名及 patch 一律依 key_version/building_key 關聯，不用 legacy_building_key。結構鍵保留縣市及行政區、路段巷弄、主號與全部子門牌，不論寫成「10之1號」或「10號之1」。無路名時另保留村里／地名。完整門牌確定後才移除樓層。明訂安全等價與版本對照，歧義的舊鍵不提供 v2 座標。定位前先加衝突回歸測試。交易與離線門牌使用主專案同一正規化函式。歷史行政區名依固定版本的 area 層與明確對照處理，不做大範圍字串替換。

無路名地址保留村里身分。子門牌與門牌區間必須依證據判斷，無法確定就覆核。保留每次展開與原始地址到地址鍵的對照。

unique_addresses.parquet 以 key_version/building_key 合併所有批次與類別，保存代表地址、類別、first_seen、last_seen、record_count 與 address_family。來源對照另存 address_occurrences.parquet，以地址鍵與 component_id 關聯，不在單一地址累積無上限的清單或字串。代表地址選擇必須可重複。address_family 包含縣市、行政區、道路、段，無路名時改用地名，僅用於批次排程。

只沿用地址鍵與證據版本仍有效的狀態，再查固定版離線索引及經座標確認的缺字補正。離線來源 commit 改變時，重新核對全部離線來源地址鍵。來源刪除或衝突會使相關定位失效或進覆核。TGOS／人工觀測保留為證據，不能自動勝過衝突的新官方來源。同一地址鍵出現不同座標證據時建立衝突，不隨意取第一筆。座標比較容許值在樣本與座標轉換測試後明訂，記入快照設定。人工已審查證據優先於未審查觀測，相同可信度的衝突暫停自動定位。座標只表示某次觀測到的門牌位置，不證明歷史建物位置。門牌重編或重新指派需別名或時間證據。

### 6. 持久地址狀態

| 資料集 | 最少欄位與用途 |
| --- | --- |
| address_index.parquet | key_version、building_key、canonical_address、lng、lat、source_kind、source_ref、evidence_id、observed_at、confidence、resolution_status，已知時保存 valid_from／valid_to |
| address_observations.parquet | 不可變離線、TGOS、人工觀測，來源快照、結果雜湊、解析版本 |
| verified_aliases.parquet | 別名與目標地址鍵版本、證據、核准者與時間、適用範圍及時間、有效或撤銷狀態 |
| tgos_results.parquet | 查詢及保留帳本，含 prepared、已確認提交、提交不明、取消與結果狀態；查詢指紋、batch_id、row_id、key_version、帳號及服務日期、保留版本、submitted_at、可空的回傳及座標、原始檔雜湊、匯入時間 |
| unmatched_addresses.parquet | 未定位地址鍵、原因、嘗試摘要與可查詢表示 |
| snapshot_manifest.json | 上一版快照、輸入來源設定與程式雜湊、各檔案雜湊及筆數、資料結構版本、完成狀態 |

先將 TGOS 結果存成觀測，再重建地址索引。座標無效或匹配不確定時進覆核。別名證據需確認完整門牌、行政範圍及適用時間。若服務只回傳道路或村里中心點，即使 Response_Address 相同也不代表同一門牌。別名鏈不得循環，且必須指向唯一有效目標。撤銷別名後，相關定位失效並重建歷史結果。

### 7. TGOS 輪次與人工交接

沿用 docs/legacy/RESUBMIT_RUNBOOK.md 的 addrCompare 網頁批次流程：人工上傳 UTF-8-sig CSV、等待 email 通知、下載結果，每日 10,000 筆且每片最多 10,000 行。既有帳號不使用即時 QueryAddr。上傳設定為 WGS84 經緯度 EPSG:4326、分單／雙號比對、誤差不限、僅回傳一筆、其餘不勾。依共用帳號的已提交／保留筆數及操作員服務日期紀錄管理，不能讓每個 agent 各算一份。這些是既有專案操作條件，不再列為 OQ。

從最新已驗證快照保留一輪地址。CSV 使用 UTF-8 BOM，欄位為 id、Address、Response_Address、Response_X、Response_Y。id 是穩定批次列識別，另附 manifest 對照 key_version/building_key 及查詢指紋。地址家族與家族內拆分順序都固定。

狀態依序為 prepared、operator_confirmed_submitted、imported、backfilled、closed。另有 submission_unknown、cancelled 與 import_rejected，必須附原因。批次清單與查詢帳本先持久提交，才輸出交接。配額按共用帳號及服務日期計入保留、已提交與提交不明筆數，家族拆分不增加配額。中斷或人工尚未回報的提交維持 submission_unknown，不重複輸出，須由人工核對。可能已提交的批次不因逾時自動釋放，提交確認也需再檢查配額。prepared 會保留地址，避免重複建批次。提交前取消才釋放保留。匯入需要原始批次清單、已知 row_id、相符查詢文字、重複及缺列核對、原始結果雜湊與明確 WGS84 聲明。舊回傳契約依 Address 對照送入地址，不要求服務回傳 id。使用 Address 與原提交清單精確一對一對照，歧義或缺列就拒絕。以樣本及實際完成檔驗證既有 Address、Response_Address、Response_X、Response_Y 欄位。不默默互換 X/Y，也不用數值大小猜投影座標。疑似座標軸或座標系問題先隔離，等待人工確認。

完全相同的失敗查詢不自動重送，但地址仍在未定位池。匯入格式錯誤不是 TGOS 查詢失敗。新的已驗證地址表示可有不同查詢指紋，人工重試須記錄前次嘗試與理由。晚到結果即使地址已在新輪次解決，仍保存觀測並檢查衝突。

原始回傳保存一次，依結果雜湊與批次識別排除重複匯入。results、index、aliases 與歷史回補一起產生可驗證快照。等待人工操作時釋放雲端 runner。下一輪產生前重新計算全部歷史未定位池。

### 8. 跨專案回補契約

輸出 address_patch.parquet 與 metadata JSON，包含 patch_id、來源執行、輸入及結果雜湊、標準門牌、座標與 CRS、地址拆解、證據及來源。只有經驗證的門牌級結果可回補。TGOS 缺少的 TOWN、VILLAGE、NEIGHBORHOOD 不得編造。能由固定版行政區資料唯一推得時補齊，否則隔離覆核。

保持原路檔欄位：
FULL_ADDR、COUNTY、TOWN、VILLAGE、NEIGHBORHOOD、ROAD、SECTION、LANE、ALLEY、SUB_ALLEY、TONG、NUMBER、X、Y。
X 是經度，Y 是緯度。拆解遵守地址專案現有格式，包括 NUMBER 的字尾與 safe_filename。來源證據另外保存，不加進既有 CSV 欄位。

地址專案新增持久 supplements/ 層。固定目前 roads 為 legacy_base，每列來源尚未完整驗證，不宣稱全是官方資料。從固定 archive／commit 建立基底，後續成功官方更新的縣市基底另記官方來源。不能從已混入 supplements 的 roads 反推基底。更新器只替換該縣市官方基底，再合併已核准補充資料產生 roads/ 並重建 road.csv。去重必須有地址鍵及證據。官方更新不得默默刪除補充地址，也不得默默覆蓋衝突座標，所有衝突要有報告。基底與補充衝突未解時停止該縣市發布或維持前版，記錄經審查的取用結果後才重建。不能只有報告卻讓前端隨意選 CSV 第一筆。

縣市更新先寫到不在追蹤 roads 內的暫存並驗證，才提交可讀版本。調整目前月更新工作流程在 continue-on-error 後 git add -A／push 的方式：選定縣市失敗就不提交該次暫存結果，只提交明確列出的完整已驗證變更。失敗不會把截斷的縣市資料放進公開 commit。讀者鎖定完整 Git commit／快照，不讀取正在改寫的工作目錄。單檔重新命名不等於整份資料原子提交。月更新與 patch 匯入共用寫入鎖或 concurrency group，不發布只完成一部分的縣市資料。

首版產生可審查 patch，以人工匯入或 PR 接收，不自動跨 repo 寫入。主專案的 GITHUB_TOKEN 不能寫地址 repo。未來跨專案 bot、憑證或自動合併須另經使用者授權，限制 repo 權限並防止互相循環觸發。patch 重複匯入檢查與下輪來源選取，不依賴 PR 是否已合併。

### 9. GIS 輸出、月下載與年度打包

首版相容目標為 GeoParquet 1.1，明確使用 CRS84 的經度／緯度順序與 WKB 幾何。PyArrow 寫入 geo metadata，DuckDB 讀取分析欄位。採用後續 GeoParquet 版本前，先驗證讀取相容性。

正式交易與位置成員分開。已證明交易識別每個一列；尚未證明時，保留以 raw_record_id 為鍵的來源觀測粒度，transaction_key 可空，manifest 與統計如實標示。成員展開不得重複交易金額。

sales／presale 一個不同定位點使用 Point。兩個以上且 bbox 寬高皆大於零時才產生包圍 Polygon；同經度或同緯度的共線點使用 MultiPoint。bbox 明確標為近似，不宣稱建物輪廓。rent 使用 Point／MultiPoint，成員另外關聯。部分定位記錄已定位／總成員數，全未定位保留 null geometry 與診斷。GeoParquet、GeoJSON、NDJSON 的 ID、粒度、筆數與 null 必須一致，metadata 反映實際 Point／Polygon／MultiPoint。

**下載的最小時間單位是交易月份，再提供年度包。** 沿用舊 README 與操作文件，以交易／租賃日期換算的西元 tx_yyyymm 分月，不依 raw 的 src_batch 季度、匯入日期或下載日期分月。不同 raw 批次中相同交易月份的記錄進入同一月檔。日期無效時另存診斷，不編造月份。

月檔保留舊版 YYYYMM_category 字首，例如 202511_sales.ndjson、202511_sales.geojson、202511_sales.parquet。GIS 的 .parquet 檔包含 GeoParquet metadata。每月 sales、presale、rent 各自提供各格式檔案，使用者可只下載需要的月份／類別／格式，不需取得整年或 raw ZIP。

預計輸出布局：

```text
data/output/<snapshot_id>/
  monthly/<YYYY>/<YYYYMM>/
    <YYYYMM>_sales.parquet
    <YYYYMM>_sales.geojson
    <YYYYMM>_sales.ndjson
    <YYYYMM>_presale.<format>
    <YYYYMM>_rent.<format>
    manifest.json
  yearly/<YYYY>/
    lvr_<YYYY>_geoparquet.zip
    lvr_<YYYY>_geojson.zip
    lvr_<YYYY>_ndjson.zip
    manifest.json
  snapshot_manifest.json
```

每個年度 ZIP 對應一種格式，收集同一 snapshot 已發布月檔及 manifest，依月份分目錄。年度包提供便利下載，不以年度大檔取代月檔。ZIP 內檔案必須與對應獨立月檔逐位元組相同。若年度 ZIP 超過當時 Release 附件上限，就提供有編號的技術分片、索引及完整下載／重組說明；邏輯單位仍是一年，獨立月檔仍可下載。分片依實測包大小決定，不假定 ZIP 會縮小 Parquet。

來源子範圍無法證明整月完整時，標記 month_coverage_status=scope_limited。選定 raw 批次全部列已處理，不代表該交易月全部來源已涵蓋。

月 manifest 記錄 snapshot／結構／地址鍵版本、類別／格式、位元組數、SHA-256、粒度、筆數、bbox、來源範圍、定位完整性與固定檔名。下載索引對應年／月／類別／格式的 GitHub 公開附件 URL 與雜湊。年 manifest 列出已包含月份、缺失月份、已確認空月份、範圍完整性、曆年完整性及檔案雜湊。尚未完成或當年度可先發布已有月份，明確標示部分年度；有年度 ZIP 不代表十二個月已完整。確認為零筆的月份明確記錄，不與尚未處理混淆。

TGOS／回補改變某月後，在新 snapshot 重建該月及對應年度包。其他月份可沿用已驗證內容雜湊。年度包依單一 snapshot 宣告的月檔版本組成，不從持續變動的 latest 指標混搭。記錄前後筆數，驗證解壓／讀回及受控 GIS 相容性，保留舊發布版本。線上資料庫載入仍是後續選用範圍。

### 10. 快照提交、並行與復原

每次命令取得輸入雜湊與 parent snapshot ID，各階段在新 run_id 下寫不可變檔案。本機暫存檔在雜湊驗證後同檔案系統重新命名。manifest 列出全部檔案，通過結構、筆數與關聯鍵檢查後才完成。讀者只讀取已提交指標，不讀半成品目錄。

GitHub 公開索引／狀態指標使用預期 Git parent／ref 核對，或明確的單一寫入排他鎖。Release 檔案不可變，先完整驗證再更新指標。若執行基於過時 parent，就不能覆蓋最新版。TGOS 保留、人工提交確認與取消也受此規則保護。保留及批次清單先提交，才公開待上傳 CSV。CAS 失敗者丟棄或重新建立未交接批次，過期寫入租約需重新確認權限及 CAS，不能直接發布。GitHub concurrency group 不能單獨保護其他 agent 工作或其他 repo。

只有程式、結構、設定、輸入雜湊相同才能重用階段結果。失敗階段不能把快照標成完成。保留前版供回復。撤銷別名或替換來源時產生新快照，不直接改發布檔案。測試提交前後中斷。內容可重複性比較排除每次改變的時間欄位，只有明訂格式 metadata 才允許位元組差異。

### 11. GitHub 公開保存與交付

使用者已選定 GitHub 公開。沿用草案的 Parquet／地址狀態、GeoParquet／GeoJSON／NDJSON 與 manifest 設計。這是已確定方向，不再列為公開範圍待決問題。

| 位置 | 內容 | 交付用途 |
| --- | --- | --- |
| Repo Git | 程式、文件、樣本、補字規則、來源／下載清單，以及實測大小／歷史成長合理的小型固定 bootstrap | agent 可 clone 的上下文與下載索引 |
| GitHub 公開 Releases | 有版本的月輸出、年度 ZIP、地址／階段狀態及重建維護所需 TGOS 批次／回傳資料 | 獨立下載的公開資料及可接續 cloud 輸入 |
| Actions artifacts | 短期樣本／除錯／資源報告 | 會到期的輔助副本，不是唯一持久輸入 |

量測實際月檔、年度包及維護狀態，再決定各檔放 Git 或 Release。raw ZIP 大小本身不決定 output 能否交付。工作目錄的大型產物維持忽略，明確列出要進 Git 的小檔。不可變 Release 快照搭配有版本的下載索引，可公開交付資料，不必把每次重建的二進位檔全部累積進 Git 歷史。

raw 可在適合的本機或 cloud 環境處理。若 raw 不方便傳送或執行前資源檢查不通過，先在該環境按批次完成正規化及離線定位，再將已驗證離線快照交付 GitHub。cloud agent 可從快照接續 TGOS 建批次／匯入／回補及月輸出。若需重新解析變更的來源欄位，仍可能要從 raw 重建。全歷史時間／RSS／磁碟是否足夠，是 agent 的技術量測工作，不交回使用者當成平台選擇問題。

下載索引及狀態指標使用 Git parent 核對／單寫入協調。先上傳不可變檔案並驗證，再提交指向這些確切檔案的索引。上傳中斷或過時 parent 提交不取代前版有效指標。憑證不進資料、來源清單或日誌，公開資料保留來源／顯名證據。本設計不另加私有資料 repo 或其他保存服務。

### 12. agent 介面、CI 與文件

預計提供 python -m lvr_pipeline，命令包含 doctor、fetch-inputs、fetch-output、ingest、normalize、export-converted、measure-output、package-output、build-pool、resolve-offline、prepare-tgos、import-tgos、backfill、export、export-address-patch、verify。目前尚無這些命令。設定包含相對路徑、來源清單、parent snapshot、記憶體與暫存上限、配額範圍及公開政策。安裝腳本不變更資料。

AGENTS.md 說明實際功能、建置測試格式指令、草稿及 legacy 的地位、憑證管理、禁止未驗證模糊共用座標、TGOS 人工界線與各階段檔案責任。提供單一固定套件安裝方式，預設不下載真實大型資料。變更 I/O 時沿用既有函式，並保留 legacy 程式參考。

main 的 push CI 只使用合成或已確認可分發的樣本，不取得資料服務或發布憑證。固定 Actions 與套件版本。PR 程式不能取得發布憑證。執行單元、整合、格式及資料結構測試，正式來源檢查另外啟動。初期全歷史驗證採人工或受控觸發，不在每個提交執行。發布以 GitHub 公開進行，需有宣告範圍內完整且已驗證的快照，以及月／年包裝檢查。部分定位需明確標示；宣稱全歷史時另須全部已盤點輸入核對。

正式文件包含架構、資料契約、來源取得、雲端操作、TGOS 交接、地址 patch、發布與復原。legacy 原文維持，僅索引說明用途。原 Drive 快照保留，另連結本次完整企劃。本文件現在提供 tasks 與 test points。實作細節在個別 task 補齊，不要求另產生 GAL 執行提示。

### 13. 離線地址處理後交付第一版 output

第一個對使用者交付的里程碑位於 Parse、Normalize 及離線地址定位之後。交付可用的離線定位月資料，附未定位記錄、證據及 manifest。剛 Parse／轉換完成的表格是內部階段快照，不是使用者要求的第一版 output。TGOS 與地址專案回補不延後首次交付。

轉換階段在 data/work/converted/snapshots/<snapshot_id>/ 保存 observations／address_components／exclusions／diagnostics／dispositions，各資料集依來源批次／類別寫 Parquet，另附 quality.json 與 manifest.json。實際檔案依 manifest 路徑讀取。保存來源列識別，每個觀測只記一次金額。尚未證明交易識別時保持空值及 record_grain=source_observation。離線定位後，交付第 9 節的三格式月輸出，另附地址索引、未定位池及接續所需階段狀態。維護狀態與一般使用者的月／年下載分開，使用一個月不必下載整個地址池。維護包包含觀測／成員、地址關聯、離線索引／結果、未定位／衝突狀態及後續回補所需固定版本。初次快照明確設定 tgos_started=false；TGOS 開始後，交接必須包含保留／查詢帳本，缺帳本就失敗，不能當成新的空配額狀態。

首次處理可涵蓋明確來源子範圍、單季或全部盤點批次，分月仍依各批來源的交易日期。scope、selected_inputs、missing_inputs 與 completeness 描述來源涵蓋，不代表百分之百定位或已證明全歷史唯一交易。離線未命中保留 null geometry／診斷，不默默刪除。年度便利包如實列出已有月份。

傳送前量測實際檔案與套件，包含工作／暫存副本。小型固定輸入在實測 repo 歷史成長合理時可進 Git；一般產生的月輸出、年度包與維護狀態放公開 Releases。Git／Release 檔案分配由實作安排，遵守已確定的 GitHub 公開方案。較大檔案仍保留月份邏輯單位，必要時使用附索引的技術分片。不把半個月標成完整，也不把 Parse 表格標成已完成離線處理。

全新 cloud 工作取得固定的 GitHub 公開離線快照 manifest，以及所需輸出／狀態附件，驗證雜湊、結構與產生器版本，再從離線結果接續，不重跑全部 raw ZIP。只有需重新解析或正規化、且快照缺少相關資訊時，才要求 raw 重建。output 大小獨立量測，不由 raw 大小推定。交接測試必須證明快照足以產生未定位候選及後續回補，不能只有少量已定位點檔。

### 14. 可直接在 cloud 執行，不要求完整 GAL 流程

本專案以文件內的 phases、tasks 與 test points 作為執行契約。cloud agent 讀取已追蹤的企劃，選擇前置工作已完成的 task，實作列出的交付內容，執行對應 test points，再提交程式與結果供審查。不要求安裝 GAL、取得 .dev 檔案、另產生執行提示，或先關閉全部待決事項才開始獨立工作。

agent 實作已知的 GitHub 公開交付與舊 TGOS 契約。各資料階段前先量測資源，初次離線處理使用可用本機／cloud 能力，後續 agent 接收已驗證 GitHub 快照。執行依前置關係及測試證據，不重新把發布政策或既有 TGOS 操作方式當成使用者問題。

每個已執行 task 記錄程式 commit、輸入 manifest 雜湊、task ID、完整命令、前置結果、test point 的 pass／fail／not-run、輸出雜湊與筆數、資源量測、限制及下一個 task。持久交接記錄 GitHub 公開產物 URL、manifest 雜湊及版本保留紀錄。已消失工作環境的本機路徑不算跨 agent 交接成功。未執行的測試或無法取得的輸出，不算驗收完成。本企劃沒有宣稱任何新版執行結果。

GAL receipt 與本機企劃鏡像只供選用紀錄。它們的全域流程檢查不作為本專案執行門檻，改用個別 task 的前置條件、測試與資料界線。既有獨立架構審查只證明當時審查的設計，不能代替新輸出路徑的實作與測試。

## 預計新增與修改的檔案

以下為整體責任範圍。P0／P1 路徑與固定環境已實作，結果見 docs/p0-foundations.md 與 docs/p1-conversion.md，其餘執行程式與資料路徑仍待實作。企劃、完整流程圖與 task 結果範本已建立。

| 專案 | 路徑 | 責任 |
| --- | --- | --- |
| 主專案 | AGENTS.md、pyproject.toml、uv.lock、scripts/setup.sh | 可攜 agent 安裝與固定套件環境 |
| 主專案 | data/sources/raw_manifest.json、address_source.json | 全部輸入來源與取得政策 |
| 主專案 | lvr_pipeline/`__main__.py`、cli.py、ingest.py、normalize.py | 命令與逐批型別化輸入 |
| 主專案 | lvr_pipeline/address.py、garbled.py、garbled_resolve.py、tx_date.py | 沿用並版本化領域規則 |
| 主專案 | lvr_pipeline/address_pool.py、offline_lookup.py、address_state.py | 全域定位與狀態快照 |
| 主專案 | lvr_pipeline/tgos.py、backfill.py、export.py、packaging.py、address_patch.py | 人工批次、歷史回補與輸出 |
| 主專案 | lvr_pipeline/snapshots.py、schemas/、config/pipeline.example.toml | 驗證、提交復原與型別契約 |
| 主專案 | tests/fixtures/、tests/expected/、tests/integration/ | 小型完整情境及預期結果 |
| 主專案 | .github/workflows/ci.yml、verify-data.yml | 無資料服務憑證的 push CI 及受控資料工作 |
| 主專案 | docs/legacy/code/、docs/legacy/tests/ | 被排除的 SQLite、離線與 TGOS 程式參考 |
| 主專案 | README.md 與 docs/{architecture,data-contract,cloud-runbook,tgos-runbook,address-patch,release-runbook}.md | 功能完成後更新正式操作文件 |
| 地址專案 | supplements/、manifests/、schemas/address-patch.schema.json | 保存不受官方替換影響的補充證據 |
| 地址專案 | scripts/import_lvr_patch.py、scripts/materialize_addresses.py | 經審查匯入 patch 與相容路檔輸出 |
| 地址專案 | scripts/update_addresses.py、build_road_index.py | 暫存更新、合併補充與衝突報告 |
| 地址專案 | tests/、.github/workflows/monthly-update.yml、docs/ | 樣本驗證與共同寫入控制 |
| 主專案 | data/work/converted/<snapshot_id>/、data/output/<snapshot_id>/monthly/、yearly/、reports/ | 內部轉換快照、月輸出、年度包及量測證據；大型產物維持忽略 |
| 主專案 | docs/task-result-template.md、tests/integration/test_offline_handoff.py | task 結果格式與不賴 raw／GAL 的 cloud 交接 |

地址基底保存布局在整合試作時確定，避免在 Git 重複存一份完整 1.47 GiB 資料。根目錄行政區檔與前端介面保持相容。模組分法依實際實作單位再細化，不要求先建立空殼。

## Phases：每個階段在做什麼

這六個階段依序解決「資料能否重建、原始列如何整理、地址如何定位、缺漏如何補齊、定位成果如何回饋、全歷史如何持續執行」。第一個使用者可下載的地理資料在 P2 交付。P1 產生內部 Parquet 快照，供後續處理使用。

| Phase | 主要目的 | 完成後能做什麼 | 目前狀態 |
| --- | --- | --- | --- |
| P0 建立可重建的處理基礎 | 固定環境、來源與資料規則，避免不同執行方式產生不同結果。 | 在固定環境用相同契約開始處理資料，驗證快照並保留前版。 | 已完成本階段驗收 |
| P1 整理原始交易資料 | 將 ZIP／CSV 轉為有型別、可追溯的觀測與地址成員。 | 不用再次解析同一份 raw，就能從已驗證快照建立地址池與離線定位。 | 已驗收樣本與 115q1，尚未跑全歷史 |
| P2 產生首版離線地理資料 | 用固定地址資料定位，產生月檔、年度包與公開交接狀態。 | 使用者可按月／類別／格式下載，agent 可接續未定位資料。 | 已完成本階段驗收 |
| P3 用 TGOS 補齊定位缺口 | 沿用人工批次查詢，驗證回傳並回補所有適用歷史記錄。 | 每輪新增定位都反映在受影響月檔與年度包，下一輪不重送相同查詢。 | T-14 已驗收；T-15～T-17 等待真實交換 |
| P4 將新地址成果回饋地址專案 | 把已驗證門牌結果保存到 taiwan-address-data，讓補充成果能持續使用。 | 後續主專案可選取新的固定地址版本，官方更新不會刪掉已核准補充資料。 | 尚未實作 |
| P5 驗證全歷史規模與持續作業 | 從樣本／單季量測擴大到完整宣告範圍，補齊操作與交接文件。 | 有證據地宣告資料涵蓋範圍，並讓下一位 agent 重建、接續或發布。 | 尚未完成 |

### P0：先建立共同規則與可靠的執行基礎

**目的：**讓處理結果可以重建、核對與復原。先固定安裝方式、輸入來源、交易粒度、日期／金額規則與地址鍵，後續階段才能使用同一套規則。

**輸入：**既有解析／補字程式、原始資料來源清單、固定版本地址來源，以及小型合成樣本。

**工作：**建立固定環境與 CI，盤點來源檔案及雜湊，定義觀測／地址成員契約與 v2 地址鍵，建立本機快照的驗證、提交及中斷保護。未證明的交易識別保持空值，來源 URI 或權利缺證據時如實記錄。

**交付與完成判斷：**固定環境、樣本、來源 manifest、資料契約及快照功能可用，相關測試通過。這一階段建立處理基礎，不交付使用者地理資料。

**對應：**T-01～T-04。驗收為 TP-01～TP-04、TP-10。證據見 [P0 紀錄](../p0-foundations.md)。

### P1：把 raw 變成後續可以處理的結構化資料

**目的：**把 ZIP／CSV 交易列整理成內部資料，保留來源與異常。後續地址定位可從快照開始，不必每次重新解析 raw。

**輸入：**已列入 manifest 且可取得的指定 raw 批次、P0 契約，以及固定版本的人工補字規則。

**工作：**逐列讀取並分批寫 Parquet，核對欄名與英文說明列，保存原始值及未知欄位。正規化日期、金額與地址，將一筆觀測與多個地址成員分開。土地、純車位、無門牌、無效日期及解析失敗都保存去向，核對筆數與關聯。

**交付與完成判斷：**已驗證的 observations、address_components、exclusions、diagnostics、dispositions，以及品質報告／manifest。來源列去向總數一致，成員沒有孤兒關聯，金額不因地址展開重複。產物是離線定位前的內部快照，還沒有使用者 GIS 月／年下載。

**對應：**T-05～T-07。驗收為 TP-02～TP-07、TP-10 的 P1 範圍。目前已驗收樣本與 115q1，證據見 [P1 紀錄](../p1-conversion.md)。全歷史量測留在 P5。

### P2：用既有地址資料產生第一版可下載地理資料

**目的：**先利用 taiwan-address-data 的固定版本完成離線定位，交付第一版實際可用的地理資料，不等待 TGOS 全部完成。

**輸入：**P1 已驗證的內部快照、固定 commit 的地址來源，以及來源／地址鍵規則。

**工作：**建立離線門牌索引與跨年份／類別的全域地址池。唯一且有效的證據可定位，歧義與衝突保留供覆核。依定位結果建立幾何，保留未定位或部分定位記錄。再依交易月份 tx_yyyymm 產生各類別／格式月檔，用同一快照的原月檔組年度 ZIP，量測大小並安排 GitHub 公開附件及索引。

**交付與完成判斷：**使用者可獨立下載需要的月份／類別／格式，也可下載年度包。下載索引、檔案雜湊、來源範圍與定位完整性可核對。另有地址索引、未定位池、來源關聯與維護狀態，讓 agent 不重跑 raw 也能接續支援的下游動作。初始狀態明確為 tgos_started=false。

**對應：**T-10～T-13 建立索引、地址池、離線狀態及 output，T-08 量測產物，T-22／T-09／T-23 完成公開取得、提交復原、cloud 接續與發布驗證。驗收為 TP-08～TP-14、TP-23～TP-26。只要第一個宣告範圍已驗證，即可交付首版，不必等全歷史。

### P3：用人工 TGOS 查詢補回未定位資料

**目的：**對 P2 未定位池中符合條件的地址逐輪查詢，將可信的新定位結果回補到所有適用歷史記錄。

**輸入：**最新已提交的地址狀態、未定位池、共用帳號配額／查詢帳本，以及人工下載的 TGOS 回傳檔。

**工作：**選取一輪候選，先提交保留帳本與批次 manifest，再交付 CSV 給操作員上傳。沿用每日／每片最多 10,000 筆、UTF-8-sig、addrCompare 與 WGS84 設定。回傳以 Address 精確對照原批次，驗證門牌、座標與別名證據。成功結果回補所有適用歷史，重建受影響月份與年度包，再重算下一輪未定位池。

**交付與完成判斷：**每輪都有可追溯的查詢／保留／回傳狀態與完整子快照。失敗、衝突或提交不明也保留，不能當成永久放棄或因逾時自動釋放配額。下一輪不自動原樣重送。此階段增加定位涵蓋，不保證所有地址都能定位。

**對應：**T-14～T-17。驗收為 TP-15～TP-18、TP-10、TP-14、TP-26。P3 是對已發布資料逐輪增補，不能阻擋 P2 首版交付。

**目前進度：**地址 repo 已更新到 `02887978ef19c1067e339787bae976a72d4723af`，真實離線重建得到 48,882 個 located、2,269 個 conflict 與 4,871 個 unmatched。T-14 已驗收；T-15～T-17 的程式與合成整合已通過，但尚未建立真實批次。建立批次前必須取得該服務日期的 `external_used`，不能推定共用帳號尚未使用額度。

### P4：讓定位成果成為可持續使用的地址資料

**目的：**將主專案找到的可信門牌成果回饋 taiwan-address-data，減少後續重複查詢，並確保官方更新不會抹掉補充成果。

**輸入：**P2 或 P3 已驗證的門牌級觀測、地址拆解與來源證據，以及固定版本的地址基底。

**工作：**產生可核對的 address_patch 與 metadata，隔離缺行政區證據的結果。地址專案持久保存 supplements，依固定基底與已核准補充重建 roads／road.csv。官方縣市更新先暫存及驗證，遇到失敗或未決衝突保留前版。patch 匯入不要求每次下載官方更新。

**交付與完成判斷：**補充資料可重複匯入而不增生重複列，官方更新後仍保留。原 14 欄 CSV 與前端介面相容，來源及衝突可追溯。主專案下一輪明確選取新地址 commit，不自動跨 repo 互相循環觸發。

**對應：**T-18～T-20。驗收為 TP-19～TP-21。P4 可從 P2 的有效證據開始，不必等待所有 TGOS 輪次結束。

### P5：驗證能否擴大到全歷史並持續維護

**目的：**確認相同處理流程在完整宣告範圍的時間、記憶體、磁碟及傳送需求，讓「可處理全歷史」有量測與核對證據。

**輸入：**前面已驗收的處理路徑、樣本／單季量測、完整來源清單、產物與維護狀態，以及實際可用的本機／cloud 執行環境。

**工作：**先量測樣本及單季，再在資源檢查通過後擴大範圍。分別記錄轉換、索引、定位、月輸出、年度打包與同時暫存需求，保留 20% 餘裕。核對全部選定來源的去向及已知空批次，補齊安裝、資料契約、下載、TGOS、地址 patch、發布與復原文件。

**交付與完成判斷：**有實測資源報告與明確涵蓋範圍，下一位 agent 可依已追蹤文件及固定版本輸入接續。宣稱全歷史前，必須核對全部來源，缺失不能當成空批次。資源不足時保留前版並提出具體量測，不能提交部分結果後宣稱完整。

**對應：**T-21、T-24。驗收為 TP-22、TP-25、TP-26。文件可從 P0 後逐步補寫，各路徑依自己的前置條件驗證，P5 不是要求先等 P4 全部結束才開始。

### 執行順序與首版交付

P0 → P1 → P2 是首版公開離線 output 的必要主線。P3 增補定位，P4 回饋地址來源，P5 驗證規模與持續維護。各 task 依自己的前置條件執行，數字不是強制排序，也不要求完整 GAL 流程。

後續本機開發使用 C:/Code/taiwan-lvr-geodata 的 main，修改、測試與 commit 後直接 push 至 origin/main。GitHub Actions 提供 Linux 驗證。較大的資料工作依實測安排執行環境，不再把雲端當成本機開發的替代工作目錄。

每版月／年 output 都揭露來源子範圍、缺月／空月／部分年度與定位涵蓋。首版不等待 TGOS、全部歷史來源或地址 repo 修改。線上服務另案選用，實測資源與操作員速度確認前不承諾日期。

## 驗收情境

| 情境 | 必要結果 |
| --- | --- |
| 缺 ZIP、雜湊變更、已知空批次 | 缺失或損壞明確失敗，已記錄空批次核對為零交易 |
| 各年份 CSV 欄位不同 | 依欄名合併型別，保留來源欄位並產生結構診斷 |
| 跨類別相同編號、修訂、缺編號 | 不跨類別合併，依來源順序選修訂，缺編號仍可追溯 |
| 三類別與不同年份共用地址鍵 | 一個有效座標回補全部適用觀測 |
| 10號之1／10號之2、甲里10號／乙里10號、舊行政區、連字號 | v2 不合併不同門牌與地名，僅合併明訂等價表示，保留行政區證據 |
| 相似道路或鄰近門牌 | 不自動共用座標 |
| 同鍵或已驗證別名衝突 | 進覆核，拒絕別名循環，撤銷反映在新快照 |
| 10,001 候選、超大家族、兩個建批次者 | 配額不超限，順序固定，保留列不重疊 |
| 失敗查詢、提交不明、新表示、明確重試 | 不自動重送原樣查詢，不自動釋放提交不明保留，仍可用新證據與核准重試 |
| 提交 Address 對照缺失／歧義或缺批次清單、軸顛倒、投影座標 | 附理由拒絕或隔離，不默默修正 |
| 重複匯入、晚到結果 | 不重複記錄或筆數，保留衝突證據 |
| 中斷或兩個過時 parent 提交 | 前版仍可讀，最多一個指標更新成功 |
| patch 匯入後官方縣市更新 | 補充仍在，roads／road.csv 相容，patch 不重複，衝突有報告 |
| 多定位成員、部分或退化幾何 | 筆數與金額不重複，bbox 標明近似，共線不同點使用有效 MultiPoint |
| 無測試網路的乾淨雲端環境 | 樣本測試不需憑證，全量下載無網路時明確失敗 |

品質報告按批次、類別、縣市核對原始觀測、正式交易、展開成員、排除類別、日期異常、定位、未定位及衝突數。記錄來源占比、每輪新增地址鍵、歷史回補數、排除重送數、唯一查詢數及資源量測。定位率如實報告，不承諾任意命中率。

## 完成條件

- TGOS 完成前交付已驗證離線月輸出及維護狀態，全新 cloud 可不重跑 raw ZIP 接續支援的下游動作。
- GitHub 提供獨立月下載及年度包，月檔雜湊、範圍及定位完整性可核對。
- cloud agent 依文件的 task 前置關係與 test points 執行，不需 GAL 或未追蹤 .dev。
- 新雲端環境能固定安裝並完成樣本整合，不需本機專用檔案或憑證。
- 58 個來源項目皆取得或明確標為已知空批次，全歷史核對每個觀測去向。
- 同一適用地址鍵跨類別與年份保持一致，不做模糊座標傳播。
- 人工 TGOS 期間可停止所有運算，下一個 agent 只靠持久狀態接續。
- 官方更新後仍保留補充地址，能追溯已驗證證據。
- 三種 GIS 格式讀回一致，來源與品質筆數完整。
- 記錄實測執行資源及已實作 GitHub 公開檔案／索引安排。
- 演練中斷復原、重複匯入、別名撤銷與過時 parent 衝突。
- 新執行路徑不含 SQLite 或舊 output 匯入。

## 風險與處理

| 風險 | 影響 | 處理 |
| --- | --- | --- |
| runner 磁碟或全域查詢記憶體不足 | ZIP 小也可能無法全量執行 | 按批次處理、暫存量測、前置檢查及階段結果 |
| 地址正規化衝突 | 錯座標傳播至歷史資料 | 地址鍵版本、歧義隔離、證據與撤銷 |
| 縣市更新覆蓋 patch | 人工 TGOS 成果消失 | 持久補充層與更新回歸 |
| 多檔案發布中斷 | 資料混用不同版本 | 不可變檔案、已驗證 manifest、排他或 CAS 提交 |
| TGOS 結果不是精確門牌 | 錯別名與錯回補 | 狀態、行政區與定位精度證據 |
| 來源／顯名證據缺失 | 來源紀錄不完整 | 輸出保留來源條件與顯名紀錄 |
| 憑證誤進公開產物 | 憑證外洩 | 無憑證樣本、排除憑證值，PR 不提供憑證 |
| agent 平台能力未知 | 雲端假設不成立 | 平台無關命令與量測比較 |
| 人工 TGOS 等待 | 完成日期無法確定 | 釋放 runner、持久佇列與明確交接 |

實際資源是否足夠及套件大小仍待量測，不宣稱已取得結果。

## 待決事項

無。GitHub 公開交付及舊 TGOS 操作規則已確定。資源量測、Git／Release 檔案安排與打包方式由 tasks 執行。

## 審查結果

### 架構審查

審查結果為 APPROVE。針對性獨立審查已確認離線首版 output、交易月下載與原月檔年度包、GitHub 公開／索引復原、既有 TGOS Address 對照及前置關係。P2 交接只驗收離線狀態，P3 才驗收實際 TGOS／回補執行。初始 tgos_started=false 與後續必備查詢帳本防止配額重置。每個 task 仍需測試／資源／憑證證據，不重新詢問使用者 OQ 或要求完整 GAL。該次規劃審查當時未執行程式測試。後續 P0／P1 的測試與單季量測已完成，範圍見階段紀錄，其餘仍為 NotRun。

<!-- ARCH_REVIEW: CLEAR -->

### 商業審查

未請求商業審查。本計畫不實作定價、入門流程、通知產品或權限系統。發布信任範圍由使用者決定，不由商業分析者代決。

### 設計審查

未請求設計審查，沒有新增客戶介面。

### 工程審查

P0／P1 的實作與測試證據見各階段紀錄。後續實作仍為 NotRun。本企劃現在定義 24 個 task 契約與 26 個 test points，沒有宣稱 ENG_REVIEW 核准或全域 GAL 交接門檻。各實作 task 依自己的前置條件及測試證據驗收。

### 文件結構檢查

文件檢查通過：六個 phases、24 個唯一 tasks、26 個唯一 test points、完整測試參照、無循環前置參照、英文／繁體中文契約對應、文字流程圖及空白格式。另審查個別決策門檻與圖文一致性。P0／P1 的執行測試狀態見階段紀錄，其餘仍為 NotRun。

### 方案精簡檢查

沿用地址、補字、日期規則及地址更新器／索引慣例。ZIP、CSV、雜湊、命令用標準函式庫，SQL 用 DuckDB，Parquet metadata 用 PyArrow。程式審查用本機 Git diff 與 CI。維持單一命令介面及不可變檔案。服務 API、自主 TGOS 瀏覽器、自動跨 repo 合併、分散式工作框架、永久資料庫留待後續。輸入驗證、持久證據、並行與復原仍是必要保護。

## Test points：驗收項目

以下 test points 是驗收條件。P0／P1 已執行的範圍與結果見階段紀錄，其餘仍為 NotRun。既有 94 個測試是基準證據，不能代替新增測試。每個相關提交執行適用樣本檢查。較大的離線／輸出工作前先做單季資源量測。GitHub 公開與舊 TGOS 設定已知；全歷史、實際結果及發布測試需具體輸入檔、實測資源與正常憑證，不重新詢問使用者 OQ。相關測試失敗會阻擋該 task 驗收，不會阻擋無關 tasks。

| Test point | 主題 | 設定與輸入 | 必要結果 |
| --- | --- | --- | --- |
| TP-01 | Linux 樣本安裝 | 全新 Linux、固定安裝、無憑證／真實資料、既有六個測試檔及新樣本流程。 | 既有 94 個基準測試仍通過，新樣本流程通過；安裝不修改／下載真實資料，記錄環境與命令。 |
| TP-02 | 來源與輸入驗證 | 缺失／損壞 ZIP、雜湊不符、101q1 已知空批次、CSV 欄位變動及英文說明列。 | 缺失／損壞明確失敗，只有有證據的空批次核對為零；未知欄位保留並診斷，說明列不算交易，選定範圍明確。 |
| TP-03 | 觀測與交易識別 | 跨類別／縣市同編號、缺 ID、有證據修訂、來源順序未知／同順位、固定欄位衝突。 | 不做未證明合併，保留 raw_record_id；未證明 transaction_key 保持空值，來源排序需證據，不依取得時間選一筆，record_grain／筆數如實標示。 |
| TP-04 | 地址鍵碰撞 | 10號之1／10號之2、等價 10之1號、無道路甲里／乙里、樓層、不明區間／連字號、09007／09020。 | 不同門牌／地名不合併，只合併明訂等價表示；不由舊鍵播入座標，歧義隔離，前導零保留。 |
| TP-05 | 逐批轉換 | 小型預期結果樣本與逐步增大的合成輸入，記錄 Arrow 批次大小；輸入途中模擬中斷。 | 來源列／關聯符合預期，不使用整份成員／類別清單；輸入增加時量測 RSS 並對照預算，中斷輸出不具有已完成指標。 |
| TP-06 | 正規化核對 | 三類別、多門牌展開、土地／車位／無門牌、缺失／零金額、無效日期及未知欄位。 | 每個原始交易列有一種保留／排除／失敗去向；診斷可重疊，但不多算交易。觀測金額只存一次，展開不改總額；日期截止與單位固定。 |
| TP-07 | 內部轉換快照 | 讀回樣本及指定 raw 批次的觀測／成員／排除／診斷 Parquet。 | 雜湊／結構／型別／筆數／外部索引鍵正確，粒度及範圍明確。Parse 表格標為內部轉換，不稱離線使用者 output。 |
| TP-08 | 輸出大小與 GitHub 安排 | 量測實際月檔、年度 ZIP、維護狀態及暫存；評估小型固定 bootstrap 進 Git，一般產物進公開 Releases。 | 檔案／傳送／工作磁碟預算依實測及當時平台限制，raw 大小不拒絕 output 交付。分片保留月／年索引與完整邏輯單位，不把大型產物強制加 Git。 |
| TP-09 | cloud 從離線 output 接續 | 全新 Linux，不提供 raw／舊檔／Windows 路徑。取得含已定位／未定位、索引、來源關聯及狀態的快照，另修改雜湊／版本或省略必要狀態。 | 宣告月份 ID／座標／筆數及未定位選取一致，保留狀態／關聯足以供後續 TGOS／回補；P2 只驗證離線對應／未定位選取，TGOS／回補整合在對應 P3 tasks 通過後執行。缺失／不相容明確失敗，只有無法由快照重建的資訊才需 raw。 |
| TP-10 | 本機快照復原 | 檔案／manifest／指標完成前後中斷，以相同輸入重試，模擬本機寫入競爭。 | 讀者只取得前版或完整新版，不取得部分完成狀態。過時／競爭寫入不能覆蓋 parent；重用需程式／結構／設定／輸入雜湊相同。 |
| TP-11 | 固定離線索引 | 固定地址 commit、前導零、無道路檔、檔名清理碰撞、重複／衝突座標及後續來源變動。 | 依欄位判讀，不只猜檔名位置；未驗證來源標 legacy_base。移除／衝突證據不自動沿用舊結果，記錄建置資源。 |
| TP-12 | 全域池與來源關聯 | 跨類別／年份共用有效鍵、高頻鍵的大量關聯、附近不同門牌。 | 每版本鍵一列，獨立來源關聯完整且代表固定；家族／距離不證明等價，不建立無界單鍵聚合。 |
| TP-13 | 離線座標證據 | 精確唯一命中、可信結果不一致、新官方來源與人工證據衝突、來源列移除。 | 唯一有效證據定位適用觀測；衝突進覆核，不任取 CSV 第一列。來源變更重查相依鍵，未定位仍核對。 |
| TP-14 | GIS 對應與幾何 | Point、正寬高 bbox、同軸共線點、租賃 MultiPoint、部分／無定位、未證明識別列。 | GeoParquet 1.1 metadata／CRS84／WKB 及受控讀取檢查正確。三格式 ID／粒度／筆數／null 相同，不產生零面積 Polygon，不重複成員金額或宣稱未證明唯一交易數。 |
| TP-15 | 共用既有 TGOS 配額 | 10,001 候選、超大家族、兩個建批次者、人工中斷、提交不明、提交前後取消。 | 沿用共用帳號每日／每片 10,000 上限，不讓 agent 各算配額；不重複交接、逾時釋放或預建未來批次，寫入失敗者不交付 CSV。 |
| TP-16 | 既有人工程序與嚴格匯入 | 舊 UTF-8-sig CSV、Address／Response_Address／Response_X／Response_Y、無 id、重複／缺地址、WGS84 設定及疑似軸問題。 | 反映既有 addrCompare 與 WGS84／單筆回傳設定。依可證明一對一 Address 匯入，不要求回傳 id；歧義／無效列拒絕或隔離，不猜軸／CRS。 |
| TP-17 | 查詢與匯入重複處理 | 相同結果重複匯入、晚到、失敗查詢、不合法匯入、新表示及明確重試。 | 原回傳／雜湊可重建，重複匯入不加列／筆數；失敗查詢與拒絕匯入分開，晚到衝突保留證據，重試記前次及原因。 |
| TP-18 | 別名回補與月／年更新 | 有效別名／中心點、循環／撤銷、跨月份／年份／類別受影響記錄。 | 只有已證明完整門牌共用座標；子快照重建受影響月及年度包，保留未變內容雜湊與前版。不重複金額或保留過時年度檔。 |
| TP-19 | patch 結構與相容 | 有效門牌證據、縣市／鄉鎮／村里缺失或歧義、字尾／檔名情境、舊 14 欄 CSV 讀取器。 | 不編造行政區值，無證據先隔離；X／Y 與代碼字串正確，來源另存 sidecar，舊 roads／前端結構可讀。 |
| TP-20 | 縣市更新安全失敗 | 補充匯入後更新、座標不一致、一個選定縣市失敗、暫存／重建途中中斷。 | 補充仍在；衝突停止提交或保留前版縣市。選定縣市有失敗就不提交該次暫存更新，發布檔完整，不 push 部分結果。 |
| TP-21 | 地址匯入並行與基底隔離 | 相同 patch 兩次、更新／匯入重疊、嘗試從含補充 roads 重建基底。 | 匯入不重複，共用寫入控制，roads／road.csv 結果固定；保留固定 legacy_base 與 supplements 區別，不把混合結果當新基底。 |
| TP-22 | 單季至全量資源驗收 | 先量測相同範圍樣本／單季，資源檢查通過後才跑全部選定清單；分別量測輸入、轉換、索引、定位與輸出。 | 記錄時間／RSS／可用磁碟／暫存／輸出並保留 20% 實測空間餘裕；超限停止／升級，不提交部分結果。核對範圍全部列及空批次，宣稱全歷史需完整來源涵蓋。 |
| TP-23 | GitHub 快照提交復原 | 公開 Release 候選附件、上傳失敗／不完整、雜湊變動、過時 Git parent、並行 agent、缺發布憑證。 | 完整附件驗證後才提交索引；前版可取得，過時寫入不覆蓋指標，不交接半批次。憑證不進公開產物。 |
| TP-24 | 公開範圍月發布 | 離線首版／TGOS 補齊版、子範圍／全來源清單、月／年附件、上傳失敗。 | 公開索引可直接選月／類別／格式，範圍／粒度／定位與雜湊／讀回正確。宣稱全歷史需核對輸入，不等待 TGOS 全完成；失敗保留舊索引，無憑證 PR 不發布。 |
| TP-25 | 不依賴 GAL 的 agent 交接 | 全新 cloud 只提供追蹤企劃／結果紀錄與 GitHub 公開合成或離線快照，支援流程不提供 GAL／.dev／提示／raw ZIP。 | 已驗收路徑可依完整 task／test／輸入／輸出 ID 與月／年索引執行。URL 無法取得或只剩到期 artifact 不算交接，未實作／待辦明確標示。 |
| TP-26 | 月最小單位與年度打包 | 202511／202601 交易跨不同 raw 季度及三類別、無效日期、空／缺月份、部分來源範圍、單月回補、超大年度包、只下載單月的使用者。 | 依交易 tx_yyyymm 分月，保留 YYYYMM_category 檔名與獨立格式下載。年度 ZIP 解壓出單一宣告快照的原月檔／雜湊，筆數核對不重複。缺／空／部分月份明確，子範圍不宣稱完整月／年。只重建受影響月與年度包；分片索引完整，單月使用者不需年包或 raw。 |

實作定義命令後，把完整測試命令及結果位置寫入 task 結果。不能把預計新增的 CLI 名稱當成現有命令執行。python -m pytest -q 仍是目前基準測試命令。先建立樣本整合，再依賴真實資料流程驗證。

## Tasks：可執行任務

P0／P1、P2 與 P3 T-14 已通過各自的階段驗收。T-15～T-17 的程式與合成整合可用，但需要真實人工交換證據才能勾選。前置條件指已驗收的交付成果，不只代表程式已寫完。

勾選表示該 task 已實作並通過本階段驗證，Git 交付使用 main 的本機提交與直接推送。T-01～T-07 的證據見 [P0 紀錄](../p0-foundations.md)與 [P1 紀錄](../p1-conversion.md)。尚未完成的 tasks 保留未勾選。

- [x] **T-01／P0**
  - 前置條件與適用門檻：無 task 前置，只用樣本。
  - 工作與檔案責任：安裝／環境試作與 cloud 指引。負責 AGENTS.md、pyproject.toml、uv.lock、scripts/setup.sh、tests/fixtures 及 CI 安裝；保留既有函式／測試。
  - 交付／test points：固定 Linux 安裝、合成 CSV／ZIP／地址樣本、無憑證測試命令。TP-01。

- [x] **T-02／P0**
  - 前置條件與適用門檻：無 task 前置；盤點允許使用的本機／公開來源，不上傳資料。
  - 工作與檔案責任：負責 data/sources/raw_manifest.json 與 address_source.json。列出 58 批來源、成員、雜湊、已知空批次與固定地址來源；無證據的 URI／權利明確留待確認。
  - 交付／test points：機器可驗證來源清單與條件／來源紀錄，不編造雲端 URI。TP-02。

- [x] **T-03／P0**
  - 前置條件與適用門檻：T-01；T-02 可用時核對來源語意，未證明識別仍採觀測層。
  - 工作與檔案責任：負責 schemas/、config/pipeline.example.toml、address.py／tx_date.py 的版本規則。定義觀測／成員／排除契約、record grain、來源識別／修訂政策及 v2 鍵。
  - 交付／test points：有版本的結構與碰撞樣本，未知正式識別維持可空值。TP-03、TP-04。

- [x] **T-04／P0**
  - 前置條件與適用門檻：T-01、T-03；只用本機或一次性單寫入工作環境。
  - 工作與檔案責任：負責 snapshots.py 與 manifest 驗證。建立不可變暫存、雜湊／筆數核對及已提交指標讀取；遠端平台／CAS 介面由 T-22 負責。
  - 交付／test points：轉換與地址狀態共用的本機快照提交／復原功能。TP-10。

- [x] **T-05／P1**
  - 前置條件與適用門檻：T-01、T-02、T-03；先用樣本，真實來源需可取得且允許使用。
  - 工作與檔案責任：負責 ingest.py 及 ingest 命令。用逐列／Arrow 批次替換 ZIP 成員清單；保留來源／成員／列關聯及未知欄位。
  - 交付／test points：指定範圍的型別化輸入分割區與筆數／結構診斷。TP-02、TP-05。

- [x] **T-06／P1**
  - 前置條件與適用門檻：T-05、T-03。
  - 工作與檔案責任：負責 normalize.py 與補字／日期／地址規則整合。觀測金額與展開成員分開，保留排除項及未解證據。
  - 交付／test points：observations／address_components／exclusions／diagnostics，來源關聯穩定。TP-03、TP-04、TP-06。

- [x] **T-07／P1**
  - 前置條件與適用門檻：T-06、T-04。
  - 工作與檔案責任：負責 export-converted 與型別化階段驗證。觀測／成員／排除／診斷 Parquet、品質／manifest 存於 data/work/converted/，用於離線處理前的內部工作。
  - 交付／test points：已驗證轉換階段快照，不稱第一版使用者 output。TP-07、TP-10。

- [x] **T-08／P2**
  - 前置條件與適用門檻：T-13；GitHub 公開已確定。
  - 工作與檔案責任：負責 measure-output 與 Git／Release 檔案安排。量測月檔、年度包、維護狀態、工作／暫存，考慮小型 bootstrap 進 Git 時另量測歷史成長。
  - 交付／test points：size_report.json、公開 Release 附件清單及明確小型 Git 檔案。TP-08、TP-26。

- [x] **T-09／P2**
  - 前置條件與適用門檻：T-13、T-08、T-22；樣本使用模擬或公開合成快照。
  - 工作與檔案責任：負責 fetch-output／verify 及全新 cloud 離線交接整合。取得公開固定 manifest、離線 output、未定位池、來源關聯及狀態；驗證雜湊／版本、離線記錄對應、未定位選取及必要保留狀態，不重跑 raw。P2 驗收不執行 TGOS／回補，該整合等 P3 tasks 驗收後進行。
  - 交付／test points：可重跑離線輸出使用端，缺所需欄位時明確要求 raw 重建。TP-09、TP-13、TP-25。

- [x] **T-10／P2**
  - 前置條件與適用門檻：T-03、T-02；使用固定地址版或合成來源，真實索引先確認可用資源。
  - 工作與檔案責任：負責 offline_lookup.py 與行政區來源驗證。從固定 CSV commit 建立型別索引，保留代碼字串、來源類別及重複／衝突觀測。
  - 交付／test points：固定索引與建置資源報告，不取得完整 Git 歷史。TP-11。

- [x] **T-11／P2**
  - 前置條件與適用門檻：T-06、T-03。
  - 工作與檔案責任：負責 address_pool.py。在指定輸入範圍建立全域唯一鍵及獨立 address_occurrences 關聯，記錄固定代表表示與排程家族。
  - 交付／test points：unique_addresses 與 address_occurrences；不以家族／鄰近座標合併識別。TP-12。

- [x] **T-12／P2**
  - 前置條件與適用門檻：T-10、T-11、T-04。
  - 工作與檔案責任：負責離線定位與首版 address_state.py。套用有效精確證據、隔離不同結果，來源變更時重新驗證來源定位。
  - 交付／test points：首版已定位／未定位／衝突狀態及定位涵蓋報告。TP-13、TP-10。

- [x] **T-13／P2**
  - 前置條件與適用門檻：T-12、T-07；不需 TGOS。
  - 工作與檔案責任：負責 export.py／packaging.py／package-output。依交易 tx_yyyymm 產生月／類別／格式檔與月 manifest，用原月檔組年度 ZIP，附年度涵蓋 manifest 及公開下載索引；發布驗證由 T-23 負責。
  - 交付／test points：首版離線定位月 output 與年度包，保留 null／部分／退化幾何及真實粒度。TP-14、TP-26。

- [x] **T-14／P3**
  - 前置條件與適用門檻：T-12、T-04；舊 TGOS 規則已知，先用合成結果。
  - 工作與檔案責任：負責 tgos.py／address_state.py 的持久查詢／保留結構，沿用 addrCompare 每日／每片 10,000 上限、共用帳號服務日期紀錄與一致狀態。
  - 交付／test points：持久 prepared／unknown／cancelled／結果狀態，不讓 agent 各算配額。TP-15、TP-17、TP-10。

- [ ] **T-15／P3**
  - 前置條件與適用門檻：T-14、T-22；操作員在 Git 外使用憑證並確認提交。
  - 工作與檔案責任：負責 prepare-tgos 與人工確認／取消，沿用 UTF-8-sig／addrCompare／WGS84 上傳程序。共用配額下先提交保留／manifest，再交付 CSV。
  - 交付／test points：一份可核對批次與既有人工清單，不預建未來日期批次。TP-15、TP-16、TP-10。

- [ ] **T-16／P3**
  - 前置條件與適用門檻：T-14；先以既有格式樣本驗證，再處理實際下載完成檔。
  - 工作與檔案責任：負責 import-tgos，用 Address 與提交清單一對一對照，不要求回傳 id。保留結果雜湊，拒絕歧義對照／軸／CRS，避免重複匯入。
  - 交付／test points：已驗證觀測、公開維護結果紀錄與拒絕／衝突報告。TP-16、TP-17。

- [ ] **T-17／P3**
  - 前置條件與適用門檻：T-16、T-11、T-04、T-13；可先用合成證據。
  - 工作與檔案責任：負責 backfill.py 與別名撤銷。子快照重建受影響交易月檔及對應年度包，保留已驗證未變月份雜湊。
  - 交付／test points：前後定位筆數、月／年一致新版本，前版可讀。TP-18、TP-10、TP-14、TP-26。

- [ ] **T-18／P4**
  - 前置條件與適用門檻：T-12 或 T-16，另需 T-03；只輸出已驗證門牌級證據。
  - 工作與檔案責任：負責 address_patch.py 與 patch 結構。輸出 patch 識別、座標／拆解及來源 sidecar；隔離無依據行政區代碼。
  - 交付／test points：可審查 address_patch.parquet、metadata 及合成相容樣本。TP-19。

- [ ] **T-19／P4**
  - 前置條件與適用門檻：T-18；另在 taiwan-address-data 提交可審查變更，先做樣本測試再發布真實補充。
  - 工作與檔案責任：負責地址 repo 補充／基底 manifest、import_lvr_patch.py、materialize_addresses.py。建立固定 legacy_base，不在 Git 重複全量資料；保留 14 欄 roads 結構。
  - 交付／test points：獨立補充層、重複匯入檢查及固定相容 roads／road.csv。TP-19、TP-21。

- [ ] **T-20／P4**
  - 前置條件與適用門檻：T-19；cloud 回歸用模擬縣市來源，真實提交需已驗證輸入與審查。
  - 工作與檔案責任：負責地址 repo update_addresses.py、build_road_index.py、monthly-update.yml。先暫存全部選定縣市、保留補充／衝突，再依共同寫入規則列出完整變更。
  - 交付／test points：可中斷復原的更新及發布門檻，不提交部分 git add -A／push 結果。TP-20、TP-21。

- [ ] **T-21／P5**
  - 前置條件與適用門檻：轉換量測需 T-07，離線輸出需 T-12／T-13；全量先通過實測資源檢查。
  - 工作與檔案責任：負責本機／cloud／GitHub Actions 路徑量測。分別量測輸入、索引、定位、月輸出、年度打包與同時暫存，再安排可行技術路徑。
  - 交付／test points：時間／RSS／磁碟／暫存／傳送／輸出報告及範圍核對。資源不足提出具體結果，不問抽象平台 OQ。TP-22、TP-26。

- [x] **T-22／P2**
  - 前置條件與適用門檻：T-04；保存平台已確定為 GitHub 公開，樣本傳送可模擬。
  - 工作與檔案責任：負責 GitHub Release 取得／上傳核對、有版本下載／狀態 manifest 及預期 parent 的 Git 指標提交。驗證上傳中斷、競爭／過時寫入及憑證界線。
  - 交付／test points：可在真實 TGOS 前使用的公開快照交接與前版復原。TP-23。

- [x] **T-23／P2**
  - 前置條件與適用門檻：T-13、T-08、T-22、T-09；發布回補版時需 T-17，宣稱全歷史另需 T-21。
  - 工作與檔案責任：負責公開 Release 驗證／工作流程。更新索引前核對月／類別／格式下載、年度原檔、manifest 範圍／粒度／筆數／雜湊及可接續狀態。
  - 交付／test points：依已確定 GitHub 方案的公開月／年候選及持久索引，TGOS 可尚未完成。TP-24、TP-26。

- [ ] **T-24／P5**
  - 前置條件與適用門檻：T-01 後即可開始文件；各路徑依自己的前置／測試驗收，不設全域 GAL 門檻。
  - 工作與檔案責任：負責 README、docs/{data-contract,downloads,cloud-runbook,tgos-runbook,address-patch,release-runbook}.md、完整資料處理流程圖及 task 結果範本。文件只記已實作路徑與待辦，保留草稿／legacy 參考。
  - 交付／test points：全新 cloud agent 可依可取得的 manifest 與 task 結果接續，不依賴本機 .dev。TP-25。

## 依據與來源

本次修訂依據為使用者在 2026-10-04 的確認：GitHub 公開、TGOS 前先交付離線處理 output、沿用交易月分檔並提供年度包，以及直接 cloud 的 phases／tasks／test points，不要求完整 GAL 流程。

| 證據 | 位置與支持內容 |
| --- | --- |
| 本機基準 | 主專案 README 與 lvr_pipeline/{0_parse_raw,1_normalize,address,garbled_resolve}.py；地址 scripts/update_addresses.py 的 run()、safe_filename()、load_area()；build_road_index.py；monthly-update.yml |
| 舊輸出／TGOS 契約 | docs/legacy/PROCESSING.md、RESUBMIT_RUNBOOK.md 及舊 repo README：交易月 YYYYMM_category 檔案、addrCompare 每日／每片上限與 WGS84 人工程序 |
| 原草稿 | docs/drafts/taiwan-lvr-geodata-新版資料處理流程.md，Drive 快照更新時間為 2026-10-03T19:17:49.383Z |
| GitHub runner | [GitHub-hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)：公開 repo 標準 Linux 規格，2026-10-04 查核 |
| 執行時間 | [Actions limits](https://docs.github.com/en/actions/reference/limits)：hosted job 最長 6 小時 |
| Release | [About releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)：附件限制與不可變快照候選方式 |
| 暫存產物 | [Workflow artifacts](https://docs.github.com/en/actions/concepts/workflows-and-actions/workflow-artifacts)：依保留設定到期，不當成唯一持久狀態 |
| 跨 repo 憑證 | [GITHUB_TOKEN](https://docs.github.com/en/actions/concepts/security/github_token)：權限限於工作流程所屬 repo |
| DuckDB | [工作量調校](https://duckdb.org/docs/current/guides/performance/how_to_tune_workloads)、[設定](https://duckdb.org/docs/stable/configuration/overview)：暫存及記憶體控制，不保證所有查詢都不會 OOM |
| GeoParquet | [1.1 規格](https://geoparquet.org/releases/v1.1.0/)、[版本清單](https://geoparquet.org/releases/)：結構、幾何、CRS，未採用 2.0 候選版 |

外部事實查核日期為 2026-10-04，實作前需重查服務限制與套件相容性。TGOS 操作條件依既有專案操作文件沿用，不宣稱重新外部查核了所有服務政策。
