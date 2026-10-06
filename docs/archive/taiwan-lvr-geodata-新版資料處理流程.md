# taiwan-lvr-geodata 新版資料處理流程

> 歷史文件：僅保留當時的設計、命令或量測，不是現行操作指示。舊完成勾選不代表重建驗收。TGOS T-16～T-18 須依新規則重驗，上游權威證明門檻的舊敘述也不作為現行要求。

現行文件見[根目錄入口](../../README.md)及[重建企劃](../plans/重建企劃.md)。原位置：`docs/drafts/taiwan-lvr-geodata-新版資料處理流程.md`。

> 狀態：草稿。本文是 Google Drive 文件的內容快照，後續企劃與正式文件將重新整理。

- 來源：[Google Drive 原始文件](https://docs.google.com/document/d/11rV4YYm-9ORDYabeITJm4t7_c-oyTR-JNL_HysBgQVA/edit)
- 來源更新時間：2026-10-03T19:17:49.383Z，UTC。
- 複製日期：2026-10-04，Asia/Taipei。
- 本文描述預計設計，不代表目前程式已完成這些功能。

目標是把實價登錄地址處理成可長期重建、可回補歷史資料、可發布多種 GIS 格式的資料管線。

## 一、核心原則

- SQLite 全面淘汰，不再作為 pipeline 的工作資料或快取。中間資料以 Parquet 保存，DuckDB 負責 SQL 查詢與批次處理，TGOS 人工批次交換使用 CSV。
- taiwan-address-data 是主要的離線地址座標來源。實價登錄中離線查不到、再由 TGOS 成功定位的新地址，由 taiwan-lvr-geodata 產生回補資料，再補回 taiwan-address-data。
- 地址解析必須跨年份、跨 sales／presale／rent 共用。新資料找到的地址要能回補舊資料。
- TGOS 每日只有 10,000 筆且需人工批次處理，因此每一輪都重新建立 unresolved pool，不預先切完後面所有批次。
- TGOS 查不到不代表永久無解。未來若出現可確認為同一地址的新表示方式，仍可由新結果回補。
### 全新執行方式

這次視為全新 pipeline，不沿用既有 SQLite、舊 output 或 migration 結果。從 data/raw/ 全部原始批次重新 Parse、Normalize，建立 Global Address Pool，再使用既有地址結果與 taiwan-address-data 做離線定位。

address_index.parquet 與 tgos_results.parquet 都由這次流程重新產生。舊結果只能做核對，不直接匯入。每輪 TGOS 結果匯入後更新 Parquet，再重新掃描全部歷史 unresolved addresses。

## 二、資料角色

### 1. 長期結果資料

- address_index.parquet：地址定位的正式結果。保存 building_key、canonical_address、lng、lat、source 等，可用來重建快取。
- 交易空間資料：以 GeoParquet 作主要分析格式，另提供 GeoJSON 與 NDJSON。
- manifest.json：記錄分檔、筆數、期間與產出版本。
### 2. 工作資料

- Parquet：作為中間資料與地址結果的主要保存格式。DuckDB 直接讀取 Parquet 執行 SQL，不要求建立長期保存的工作資料庫。
- unique_addresses.parquet、unmatched_addresses.parquet：保存地址去重與離線查詢後的中間結果，可重新產生。TGOS 人工上傳與下載使用 CSV。
- tgos_results.parquet：保存已送 TGOS 的 building_key、查詢狀態與回傳結果，作為已查詢紀錄，避免完全相同的地址無意義重送。查不到不等於永久無解。
## 三、整體流程

```text
原始實價登錄
→ Parse／Normalize
→ Global Address Pool
→ 已知地址結果套用
→ taiwan-address-data Offline Lookup
→ Global Unresolved Pool
→ TGOS 10K Batch
→ 匯入 TGOS 結果
→ 產生 taiwan-address-data 回補 patch
→ 全歷史重新比對與回補
→ Canonical Geodata
→ GeoParquet／GeoJSON／NDJSON／Supabase
```

## 四、處理步驟

### Step 0：讀取所有實價登錄批次

原始 zip 仍依 sales、presale、rent 分流，土地與純車位依既有規則另外處理。新季度可以單獨 ingest，但建立地址池與產出結果時必須能看到全部歷史批次。

### Step 1：地址正規化與 building_key

沿用目前 building_key() 的設計，統一全形／半形、台／臺、路街段序數、里鄰、樓層與部分子門牌表示。building_key 是跨年份與跨 category 重用座標的第一層鍵。

### Step 2：建立 Global Address Pool

把全部歷史 sales、presale、rent 的地址合併後，以 building_key 去重。這個 pool 是 TGOS 與歷史回補的工作母集合。

- building_key
- 代表地址 representative_address
- 出現 category
- first_seen／last_seen
- record_count
- address_family，例如縣市＋鄉鎮市區＋道路＋段
### Step 3：先套用既有地址結果

先以 building_key 對 address_index.parquet 做 exact lookup。相同 building_key 不論年份或 category 都直接共用座標。

例：2025 presale 當時未定位，但 2026 sales 出現相同 building_key 並成功定位。下一次全歷史重建時，2025 presale 必須自動取得同一座標。

### Step 4：Offline Geocoding

尚未定位的 building_key 先使用 taiwan-address-data 做離線查詢，再進行現有 garbled resolve。成功結果寫入 address_index.parquet。只有 offline miss 才進 TGOS 候選。

### Step 5：建立當日 TGOS Batch

每次只根據最新 unresolved pool 產生一份最多 10,000 筆的批次。不要一次預先產生未來十幾個 input 檔，因為前一批成功結果可能會讓後續大量地址直接被回補。

- 先排除已經有座標的 building_key。
- 以 tgos_results.parquet 排除完全相同且已經送過 TGOS 的 building_key，除非人工指定 retry。
- 依 address_family 分群，同一路段或同一地址家族盡量在同一輪一起送。若一個 family 超過 10,000，再於該 family 內分批。
- address_family 只用來決定查詢批次，不可直接當成同一地址的證據。
### Step 6：匯入 TGOS 結果

成功結果寫入 address_index.parquet 與 tgos_results.parquet。TGOS 回傳的 Response_Address 一併保存，作為後續 canonical address 與等價地址判斷的證據。同時產生符合 taiwan-address-data 現有資料結構的回補 patch，由 taiwan-lvr-geodata 流程回補 taiwan-address-data。

查不到的 building_key 也記入 tgos_results.parquet，避免原封不動重送，但仍保留在 unresolved pool 中等待未來由其他成功地址回補。

### Step 7：地址等價與歷史回補

每匯入一輪 TGOS 結果，就重新掃描全部歷史 unresolved addresses，不只處理當期資料。

- 相同 building_key：直接自動套用。
- 不同 building_key，但正規化後的 TGOS Response_Address 可證明為同一門牌：建立 verified alias 後套用。
- 人工已確認的地址別名：可套用。
- 只有字串相似、同一路段或門牌接近：不可直接共用座標，只能作為候選。
因此 2026 新出現的 sales 地址若提供了更完整或 TGOS 可辨識的表示方式，可以反向解決 2025 presale／rent 的舊 miss，而不需要再查一次舊交易。

### Step 8：重新建立 unresolved pool，再產下一個 10K

完成回補後重新計算 unresolved unique building_key。下一個 TGOS batch 從新的 pool 產生，而不是沿用昨天預先切好的第二片。這是減少人工 batch 次數的核心。

### Step 9：產出正式 GIS 結果

- GeoParquet：主要完整資料集，適合 DuckDB、Python、QGIS 與大量分析。
- GeoJSON：GIS／Web Map 交換與教學使用，可依月份或 category 分檔避免單檔過大。
- NDJSON：串流處理、開發者與既有 pipeline 相容用途。
- Supabase：線上 bbox／時間／category 查詢服務，不取代可下載的正式資料檔。
## 五、每輪 TGOS 的實際循環

```text
最新 address_index.parquet
+ 最新 taiwan-address-data
+ 全部歷史地址
→ 重建 unresolved pool
→ 依 address_family 挑最多 10,000 個 unique building_key
→ 人工 TGOS
→ 匯入成功結果與 Response_Address
→ 更新 address_index.parquet + tgos_results.parquet
→ 產生 taiwan-address-data 回補 patch
→ 全歷史 exact／verified alias 回補
→ unresolved pool 縮小
→ 下一輪再重新挑 10,000
```

## 六、完成條件

- 同一 building_key 在不同年份與 sales／presale／rent 間只需要成功定位一次。
- TGOS 成功結果可以回補所有歷史實價登錄資料，並產生 patch 回補 taiwan-address-data。
- 相似地址集中進同一 TGOS 輪，但不因模糊相似而錯誤共用座標。
- 完全相同且已查過的 TGOS 地址不會反覆消耗額度。
- 地址結果與中間資料以 Parquet 保存，DuckDB 直接讀取 Parquet 執行 SQL，不依賴 SQLite，也不要求固定的 DuckDB 安裝方式或長期保存的 .duckdb 工作資料庫。
- 每次 TGOS 匯入後都重新計算 unresolved pool，讓每日 10K 的成果立即減少後續人工批次。
