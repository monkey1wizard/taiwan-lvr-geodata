# 資料處理管線（PROCESSING）

**給資料維護者**：說明如何把內政部實價登錄 zip 轉成地理編碼 NDJSON。
消費者只需使用 `data/output/`，不需閱讀本文件。

---

## 前置需求

```
Python 3.11+
tqdm（進度條；Step 2 使用）
taiwan-address-data（離線門牌庫，選用；設定 ADDR_DB_DIR）
```
---

## 名詞解釋

- **PUA (Private Use Area, 私用區)**：指 Unicode 中保留給組織自訂的字元編碼區段。台灣政府開放資料中的罕見字或「造字」常對應到此區段（如 `U+E000`–`U+F8FF`），導致一般系統無法正確顯示或辨識，因此本專案內需要進行「造字補正」轉換為標準字。
- **`?` 缺字 (U+003F)**：上游（內政部）產製資料時無法表示的字，被替換成字面問號 `?`。與 PUA 不同——PUA 同碼位恆為同一字（可全域代換），`?` 是**多字塌縮成同一符號、原字已毀**，故無法寫 char 規則，只能靠路名語料 wildcard+座標 probe 還原（單一 `?` 落路名者），或人工 token 規則補正；落在門牌號者多屬不可復原。
- **garble**：本文件統稱「`?` 缺字 ∪ PUA 造字」；偵測由 `address.is_garbled()`（`GARBLE_RE = [?` + `U+E000`–`U+F8FF` + `]`）統一負責。

---

## 管線概覽（0–6 步）

```
data/raw/*.zip
    │
    V
[0] 0_parse_raw
    ├─> data/work/meta_{sales,rent,presale}.csv  (raw_address = zip 原值，含 PUA)
    ├─> registry/land.csv    (純土地，不進地理編碼)
    └─> registry/parking.csv (純車位，不進地理編碼)
    │
    V
[1] 1_normalize  ← 靜態造字補正（garbled_override.csv：char/variant/token）
    ├─ 原地回寫 meta.raw_address（補正後）
    └─ 依 is_garbled() 切兩輸入檔：
         offline_in/{cat}_clean.csv    （無 garble → exact lane）
         offline_in/{cat}_garbled.csv  （仍含 ?/PUA → resolve lane，帶 id）
    │
    V
[2] 2_geocode_offline  <─ taiwan-address-data 離線門牌庫（單一 script，--mode 切換）
    ├─ --mode resolve（讀 _garbled.csv，語料 wildcard + 座標 probe 破解 ?/PUA）
    │     ├─ 命中  ──> geocode.sqlite (offline_db) + 以 id 回寫 meta
    │     ├─ 有候選無座標 ──> garbled_auto_review.csv（人工複核）
    │     └─ 破不掉 ──> {cat}_garbled_pending.csv（人工補正字）
    └─ --mode exact（讀 _clean.csv，全域索引精確查找）
          ├─ 命中  ──> geocode.sqlite (offline_db, 含 FULL_ADDR)
          └─ 未命中 ─> {cat}_miss.csv (id,building_key,address)
    │
    V
[3] 3_prepare_tgos  ← 讀 {cat}_miss.csv（garble-free 候選）
    ├─ 保留 cache-already-present / 跨類 dedup / unresolvable / _dedup_prefix / chunk
    ├─> tgos_input/{cat}_input_N.csv  (UTF-8-sig, ≤10k/片)
    └─> {cat}_excluded.csv            (殘留 garble 最後防線，人工 garbled_override)
    │
    ✋ TGOS 人工節點
       上傳 tgos_input/ ─> 下載比對結果 ─> 存 tgos_done/
    │
    V
[4] 4_import_tgos
    └─> geocode.sqlite (source=tgos, 含 Response_Address)
    │
    V
[5] 5_generate  (meta × geocode.sqlite)
    ├─> data/output/{tx_yyyymm}_{cat}.ndjson  (已定位，公開)
    ├─> data/work/unlocated.csv               (cache miss，排除 unresolvable)
    ├─> registry/no_doorplate.csv             (無門牌號，park)
    └─> data/work/bad_dates_review.csv
    │
    V
[6] 6_load_supabase
    └─> Supabase public.lvr_points
```

目前無 `run.py` orchestrator；各 step 個別執行。階段一（原始資料 → TGOS 切片）的調用順序：

```bash
python -m lvr_pipeline.0_parse_raw [batch]
python -m lvr_pipeline.1_normalize [batch]            # 靜態補正 + 切 clean/garbled
python -m lvr_pipeline.2_geocode_offline --mode resolve   # 破 ?/PUA、回寫 meta（須先於 exact）
python -m lvr_pipeline.2_geocode_offline --mode exact      # clean 精確查找 → {cat}_miss.csv
python -m lvr_pipeline.3_prepare_tgos                  # miss → TGOS 切片
```

> 次序要點：`resolve` 與 `exact` 吃互斥輸入（garbled / clean），先後自由，但**兩者都須先於 `3_prepare_tgos`**，且 `resolve` 的 meta 回寫須先於 Step 5。

---

## 共用模組（helper 子腳本）

各 Step 主腳本呼叫下列共用模組；圖中以 `‹module.func()›` 標註出現位置。

| 模組 | 公開函式 | 角色 | 被誰呼叫 |
|---|---|---|---|
| `address.py` | `building_key()` | 棟級唯一鍵（定位、去重、聚合的主鍵）| Step 2/3/5 |
| | `norm()` | 全半形、巿→市、台→臺 | building_key 內部、garbled_resolve |
| | `arab_to_cjk()` | 路/街/段前阿拉伯序數→國字 | building_key 內部、garbled_resolve |
| | `_dedup_prefix()` | 去重複行政區前綴 | building_key 內部、Step 3 |
| | `parse()` | addr→(county_code, town, road, tail) | garbled_resolve、_OfflineProbe |
| `tx_date.py` | `roc_to_tx_yyyymm()` | 民國交易日→`tx_yyyymm` 分區鍵 | Step 0 |
| `garbled.py` | `load_garbled()` / `fix_garbled()` | 靜態造字補正（char/variant/token；token `?` 比 literal ?+PUA）| Step 1 |
| `address.py` | `is_garbled()` | garble 偵測（literal ? 或 PUA）| Step 1 切檔、Step 3 排除 |
| `garbled_resolve.py` | `resolve()` 等 | 兩語料補字（wildcard + 座標 probe）| Step 2 `--mode resolve` |
| `geocode_cache.py` | `connect`/`upsert`/`upsert_many`/`get_many`/`missing` | SQLite cache 讀寫 | Step 1/2/3/4/5 |

### 核心：`address.building_key(addr)` 內部邏輯

```
addr（補正後地址）
│
├─ norm()          全形→半形、巿→市、台北/中/南/東→臺
├─ arab_to_cjk()   路/街/大道/段 前阿拉伯序數→國字（2段→二段；巷/弄/號不動）
├─ _dedup_prefix() 去重複行政區前綴（北投區臺北市北投區→臺北市北投區）
│
├─ 非門牌？（_REJECT_RE 地號/地段/等N筆、_JUNCTION_RE 路口、_VAGUE_SUFFIX 對面/旁/附近/口）
│       └─是──────────────────────────────────────► 回傳 "" ⏹（不定位）
│
├─ _CITY_RE 命中 → 去里鄰（_LI_LIN_RE）
├─ 子門牌修正：3一3號→3之3號（_YI_TO_ZHI_RE）；N-M號 且 M<N→N之M號（_DASH_ZHI_RE）
├─ 截到最後一個「號」；_FLOOR_RE 去樓層/棟（…樓 / A棟）
│
└─ 結尾是「號」？
       ├─否──► 回傳 "" ⏹
       └─是──► 回傳棟級鍵 ✅
```

---

## 步驟說明

### Step 0 — `0_parse_raw`

```
一筆交易（zip CSV row）
│  raw_address = 土地位置建物門牌（zip 原值，含 PUA，不正規化）
│  tx_yyyymm ← ‹tx_date.roc_to_tx_yyyymm(交易年月日)›
│  id = {batch}-{county}-{編號 或 流水號}
│
├─【交易標的 = 土地】──────────────────► registry/land.csv（土地）⏹ 不進地理編碼
├─【交易標的 = 車位】──────────────────► registry/parking.csv ⏹
│
└─ 建物（其餘）→ ‹0_parse_raw._classify(raw_address)›
    │
    ├─ _LAND_RE 命中（地號/地段/等N筆）──► registry/land.csv（地號）⏹
    │
    └─ 否 → 多門牌偵測
         ├─ _expand_list（、，；…多個「號」）─► 展開 N 列，id=#0..#N，共用 group_key
         │      └（路名前綴補縣市區用 ‹address._CITY_RE›）
         ├─ _RANGE_RE（N-M號 且 M>N）───────► 展開 2 列（頭/尾），共用 group_key
         └─ 單一 ──────────────────────────► 1 列，group_key = id
                                              │
                                              └─► data/work/meta_{sales,rent,presale}.csv ✅
```

- 輸入：`data/raw/*.zip`（內政部 zip，per-quarter 下載）
- 輸出：`data/work/meta_{sales,rent,presale}.csv`
- 分流：`data/registry/land.csv`（純土地）、`parking.csv`（純車位）→ park，不進後續步
- `raw_address` = `土地位置建物門牌` **zip 原值**（含 Unicode 私用區 PUA 造字，不做任何正規化）；造字補正由 Step 1 負責

### Step 1 — `1_normalize`

```
raw_address（meta，含 ?/PUA）
│
├─【靜態補正】‹garbled.fix_garbled(addr, 規則←garbled.load_garbled(garbled_override.csv))›
│     命中？──是──► 回寫 meta ✅（char/variant/token；token 「?」萬用字同時比 literal ? 與 PUA）
│
└─【切檔】依 ‹address.is_garbled(addr)›（含 literal ? 或 PUA）：
      ├─ 無 garble ──► offline_in/{cat}_clean.csv   （→ Step 2 exact lane）
      └─ 含 garble ──► offline_in/{cat}_garbled.csv  （→ Step 2 resolve lane，帶 id 供回寫）

自動破字（兩語料 wildcard + 座標 probe）已移至 Step 2 `--mode resolve`。
里：只由 taiwan-address-data 出。
```

- 輸入：`data/work/meta_{sales,rent,presale}.csv`（raw_address 含 ?/PUA）
- **靜態補正**：讀 `data/registry/garbled_override.csv`，套用 char/variant/token 三 kind。token 規則的 `?` 為萬用字，**同時比對 literal `?`（U+003F）與 PUA**；防誤改靠 `scope` 守門。
- 原地回寫 meta，冪等；`building_key` 全程以補正後地址計算
- **切檔**：補正後依 `address.is_garbled()` 把每筆分流（覆寫）→ `offline_in/{cat}_clean.csv`（無 garble）與 `{cat}_garbled.csv`（含 garble，帶 `id`）
- 自動破字已移至 Step 2 `--mode resolve`（見下）；本步不再做語料比對
### Step 2 — `2_geocode_offline`（單一 script，`--mode exact|resolve`）

**`--mode exact`（預設）— clean lane**

```
{cat}_clean.csv 列 raw_address（無 garble）
│
├─ bk = ‹address.building_key(raw_address)› 為空？──是──► skipped ⏭（非門牌）
│
└─ 否 → 全域記憶體索引 addr_index[bk]（O(1) lookup，build_global_index ← roads/*.csv）
        ├─ 命中 → (lng, lat, FULL_ADDR含里鄰) → ‹geocode_cache.upsert_many(offline_db)› ✅
        └─ 未命中 ──► {cat}_miss.csv（id, building_key, address）⏭（→ Step 3）
```

**`--mode resolve` — garbled lane（破 ?/PUA，原 Phase 2）**

```
{cat}_garbled.csv 列 raw_address（含 ?/PUA）
│
└─【兩語料離線比對】‹garbled_resolve.resolve(...)›：city+site_id 範圍，garble 當萬用「.」
     │  ① ‹build_offline_road_set(taiwan-address-data)›  ② ‹load_35321(roadnames_35321_*.csv)›
     │  probe ← _OfflineProbe（per-road lazy；不建全域索引）
     │
     ├─ Tier 1：候選代入後整棟離線命中座標（鐵證）
     │     ├─► ‹geocode_cache.upsert(offline_db)› ✅
     │     └─► 以 id 回寫 meta_{cat}.csv（補正後 raw_address）✅
     ├─ Tier 2：有候選無座標佐證 ──► garbled_auto_review.csv（人工複核）⏭
     └─ none：無候選 / 多 garble / 非路名 garble ──► {cat}_garbled_pending.csv（人工補正字）⏹
            （永不自動寫 garbled_override.csv；座標命中為唯一自動套用憑證）
```

- **exact lane**：輸入 `offline_in/{cat}_clean.csv`；全域索引一次掃完 `roads/*.csv` 建 `building_key → (lng,lat,FULL_ADDR)` dict（O(1)）；命中批次 `upsert_many`（WAL + synchronous=NORMAL）；未命中寫 `{cat}_miss.csv`（覆寫）。
- **resolve lane**：輸入 `offline_in/{cat}_garbled.csv`；只處理**單一 garble 落在路名**者；比對前過 `norm`/`arab_to_cjk`；**不建全域索引**（garbled 量小，per-road lazy probe）。
- 次序：`resolve` 的 meta 回寫須先於 Step 5；`resolve`/`exact` 吃互斥輸入，先後自由，但都須先於 Step 3。

**CLI 參數**：

| 參數 | 預設 | 說明 |
|---|---|---|
| `--mode {exact,resolve}` | `exact` | exact = clean 精確查找；resolve = garbled wildcard+probe |
| `-j, --workers N` | `1` | exact 建索引的並行核心數；`0` = `os.cpu_count()` |
| `-b, --batch-size N` | `0` | exact pending 累積 N 筆 flush；`0` = 全收完一次寫 |

```bash
python -m lvr_pipeline.2_geocode_offline --mode resolve        # 先破 ?/PUA、回寫 meta
python -m lvr_pipeline.2_geocode_offline --mode exact -j 8     # clean 8 核建索引
python -m lvr_pipeline.2_geocode_offline --mode exact -j 2 -b 100000  # 低 RAM
```

> 啟用 resolve 的語料②條件：`data/reference/roadnames_35321_*.csv` 存在且 `ADDR_DB_DIR` 可讀；缺則僅用語料①。

> **RAM 注意**：記憶體大頭是全域索引本身（約 524 萬 unique key ≈ 1.5–2.5 GB），`-b` 控制不到它，只壓得住 pending 緩衝。低 RAM 機器除了 `-b`，也應降低 `-j`（每個 worker 是獨立 process，各有複製成本）。索引固定成本若仍吃不消，需改回 per-road lazy load（另案）。

### Step 3 — `3_prepare_tgos`

```
{cat}_miss.csv 列（id, building_key, address；Step 2 exact lane 未命中、garble-free）
│
├─ bk（取檔內欄，不重算）為空 / 本次已見（跨類別去重）/ 在 unresolvable.csv？──是──► 跳過 ⏭
│
└─ 否（首見 bk）→ addr = ‹address._dedup_prefix(address)›（新竹市新竹市→新竹市）
    │
    ├─ ‹address.is_garbled(addr)›（? 或 PUA，最後防線）？──是──► 暫入 excluded 桶
    │                                              └─否──► 暫入 queued 桶
    │
    └─ 收尾過濾：‹geocode_cache.get_many(bks)› → bk 已 cached？（exact 只查索引、不查 cache，故必做）
            ├─ 是 ──► cached（兩桶皆剔除，不輸出）⏭
            └─ 否 ──► 寫出：
                      queued   → tgos_input/{cat}_input_N.csv（≤10k/片, UTF-8-sig）✅
                                 欄位 id,Address,Response_Address,Response_X,Response_Y
                      excluded → tgos_input/{cat}_excluded.csv（人工 garbled_override）⏭
```

- 輸入：`{cat}_miss.csv`（Step 2 exact lane 產出，已 garble-free、已 offline-miss）；`bk` 直接取檔內 `building_key` 欄，**不重算**（與 Step 2 兩端一致）
- **cache-already-present 過濾（關鍵）**：exact lane 只查 offline 索引、不查既有 cache；Step 3 必須續用 `get_many` 剔除已 cache（TGOS/manual/migrated）的 bk，否則重送 TGOS
- 保留：跨類別 building_key 去重、`unresolvable.csv` 過濾、`_dedup_prefix`、`CHUNK_SIZE` 切片
- 輸出 `tgos_input/{cat}_input_N.csv`：欄位 id,Address,Response_Address,Response_X,Response_Y；`id`=meta 交易 id；Response_* 留空
- 殘留 garble（?/PUA，理論上不該有）→ `{cat}_excluded.csv`（人工 garbled_override）

✋ **TGOS 人工節點**：詳細操作步驟見 [`RESUBMIT_RUNBOOK.md`](./RESUBMIT_RUNBOOK.md)。

### Step 4 — `4_import_tgos`（尚未實作）

```
data/work/tgos_done/*.csv 每列
│  id(=building_key), Response_Address, Response_X, Response_Y
│
├─ Response 座標有效？──否──► 跳過（TGOS 未定到，留 cache miss）⏭
│
└─ 是 ──► ‹geocode_cache.upsert(id, lng, lat, source=tgos, full_addr=Response_Address)› ✅
```

- 讀 `data/work/tgos_done/` 所有完成 CSV
- 以 `id` 欄（=building_key）為快取鍵
- 取 `Response_Address`＋座標 → upsert `geocode.sqlite`（source=`tgos`）

### Step 5 — `5_generate`（尚未實作）

```
meta 列 → bk = ‹address.building_key›  ⋈  ‹geocode_cache.get_many(bks)›
│
├─ bk 為空（無門牌號）？──是──► registry/no_doorplate.csv（park，待另法）⏭
│
├─ cache miss（查無座標）？──是──► data/work/unlocated.csv ⏭
│         （排除 unresolvable.csv 已知無解）
│
└─ 命中 → address 欄優先序
          ① TGOS Response_Address ② 離線 FULL_ADDR ③ raw_address（補正後）
          │
          └─ 依 group_key 聚合幾何：
             ├─ rent ───────────────────► 各自獨立 point
             ├─ sales/presale 命中 1 點 ─► 退化 point
             └─ sales/presale 命中 ≥2 點 ► bbox polygon（geom_type=bbox）
                          │
                          └─► data/output/{tx_yyyymm}_{cat}.ndjson ✅
          （日期異常 → data/work/bad_dates_review.csv）
```

- 輸入：`data/work/meta_*.csv` × `data/cache/geocode.sqlite`
- 輸出 `address` 欄優先序：
  1. TGOS `Response_Address`（有值優先）
  2. 離線庫 `FULL_ADDR`（含里鄰）
  3. `raw_address`（1_normalize 補正後，非 PUA 原文）
- 輸出：
  - `data/output/{tx_yyyymm}_{cat}.ndjson`（有門牌且已定位）
  - `data/work/unlocated.csv`（cache miss，排除 unresolvable.csv 已知無解）
  - `data/registry/no_doorplate.csv`（無門牌號，park 待另法）
  - `data/work/bad_dates_review.csv`
- 多門牌（同 group_key）：sales/presale ≥2 點 → bbox polygon；1 點 → point；rent → 各自 point

### Step 6 — `6_load_supabase`（選用，尚未實作）

```
data/output/{tx_yyyymm}_{cat}.ndjson 每筆 record
│  需環境變數 $SUPABASE_URL + $SUPABASE_SERVICE_KEY
│
└─► Supabase public.lvr_points  upsert（依主鍵冪等）✅
```

- 把 NDJSON upsert 至 Supabase `lvr_points` 表
- 需 `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` 環境變數

---

## 複數地址（多門牌）處理

原始 `土地位置建物門牌` 有兩種複數格式：
- **清單**：頓號/逗號列舉，如 `府前街38號、38之1號、38之2號`
- **區間**：N-M號 且 M>N，如 `府前街38-42號`

### Step 0 — 展開

偵測複數格式 → 展開成多列，每列各自一個 building_key，共用同一個 `group_key`（= 原始交易 id）：

| id | group_key | building_key | raw_address |
|---|---|---|---|
| `115q1-k-XXX#0` | `115q1-k-XXX` | `臺中市豐原區府前街38號` | `府前街38號、38之1號、38之2號` |
| `115q1-k-XXX#1` | `115q1-k-XXX` | `臺中市豐原區府前街38之1號` | `府前街38號、38之1號、38之2號` |
| `115q1-k-XXX#2` | `115q1-k-XXX` | `臺中市豐原區府前街38之2號` | `府前街38號、38之1號、38之2號` |

### Step 2–4 — 各自定位

每個 building_key 獨立走離線庫 / TGOS，各自進 geocode.sqlite。

### Step 5 — 依命中數輸出

| 命中數 | 類別 | 輸出 |
|---|---|---|
| ≥ 2 | sales/presale | bbox polygon（外接軸對齊矩形，geom_type=`bbox`）|
| 1 | sales/presale | 退化 point |
| 0 | 任何 | unlocated.csv |
| 任意 | rent | 各自獨立 point |

---

## Registry 決策指引

| registry 檔案 | 何時寫入 | 說明 |
|---|---|---|
| `unresolvable.csv` | 確認永久無解（舊地名/已重劃/錯誤鍵入）| 格式：`quarter,Address,鄉鎮市區,category,ids,reason,noted_date` |
| `garbled_override.csv` | 造字/缺字有已知正字 | 欄位：`garbled,correct,scope,kind,evidence,noted_date`；kind=char/variant/token；人工維護，勿以程式寫入 |
| `data/work/garbled_auto_review.csv` | Step 2 resolve Tier 2 自動產出 | 欄位：`raw_address,candidate,evidence,source_file`；兩語料庫有候選但無座標確認；人工覆核後可手動升級進 `garbled_override.csv` |
| `data/work/{cat}_garbled_pending.csv` | Step 2 resolve「none」自動產出 | 欄位：`id,raw_address,evidence`；resolver 無候選（多 garble/非路名/門牌號毀字）；人工查正字補進 `garbled_override.csv` 重跑，或確認已毀記 `unresolvable.csv` |
| `land.csv` / `parking.csv` | 純土地/車位（0_parse_raw 自動）| park，不進地理編碼 |
| `no_doorplate.csv` | 無門牌號（4_generate 自動）| park，待另法 |

> **token 規則與 Step 2 resolve 的關係（退役原則）**：`garbled_override.csv` 的 **token** kind 是「Step 2 `--mode resolve`（roads_35321 語料 + 座標 probe）**解不出**時」的人工補集。判斷一條 token 是否該存在：
> - **路名** token 且其正字能被 resolve lane 逐筆座標確認 → **冗餘，應退役**，交回 Step 2 resolve（逐筆座標確認比盲套 scoped 通則可靠）。退役後該模式由 Step 1 靜態補正改為 Step 2 resolve 處理。
> - **char**（PUA→正字，多為非路名）、**variant**（異體字正規化）、**村里/地名** token → resolve `parse()` 只認路名，結構上碰不到 → **保留**。
>
> 退役核對工具（一次性，可刪）：`python -m lvr_pipeline.tmp_reconcile_overrides` 產 redundant/keep 報告；`--cache-audit` 揪盲套 token 寫入 cache 的不可驗證座標。實測 8 條 token 中 3 條路名（`?榔七街`/`?榔二街`/`榴?十二街`）可退役。

---

## geocode.sqlite cache source 說明

| source | priority | 寫入時機 |
|---|---|---|
| `manual` | 6 | 人工查座標後直接 upsert |
| `tgos` | 4 | 4_import_tgos 回灌 |
| `offline_db` | 3 | 2_geocode_offline `--mode exact` 命中，或 `--mode resolve` Tier1 補字後命中 |
| `output_old` | 2 | tmp_rehydrate_from_output（一次性舊版資料回灌，已完成） |
| `migrated` | 2 | 舊版 cache migrate（已完成） |

---

## 手動座標流程

當地址可人工查到座標（Google Maps / 電子地圖），但 TGOS 定不到時：

```python
from lvr_pipeline.geocode_cache import connect, upsert
from lvr_pipeline.address import building_key

addr = "彰化縣彰化市岸頭巷15弄38號"
bk = building_key(addr)
con = connect()
upsert(con, bk, lng=120.547417, lat=24.059353, source="manual")
```

---

## 快速重建 115q1

```bash
# 階段一（首次或新季 zip）
python -m lvr_pipeline.0_parse_raw 115q1
python -m lvr_pipeline.1_normalize 115q1
python -m lvr_pipeline.2_geocode_offline --mode resolve
python -m lvr_pipeline.2_geocode_offline --mode exact
python -m lvr_pipeline.3_prepare_tgos

# 人工：上傳 tgos_input/*.csv → 下載完成檔 → 存入 tgos_done/

# 階段二（Step 4/5/6 尚未實作；待補）
```

---

*TGOS 金鑰詳情見記憶體筆記 `tgos-api-ip-locked.md`：金鑰綁 addrCompare 批次服務，非即時 API。*
