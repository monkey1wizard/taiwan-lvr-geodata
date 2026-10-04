# TGOS 批次上傳 Runbook

> **操作備忘**：管線遇到 TGOS 人工節點時的操作步驟。
> 標準管線見 [`PROCESSING.md`](./PROCESSING.md)；TGOS 金鑰說明見 memory `tgos-api-ip-locked.md`。

**即時 QueryAddr API** 帳號無法申請（`3_geocode_tgos.py` 無效），
定位一律走 **addrCompare 網頁批次**：手動上傳 UTF-8 切片 → email 通知 → 下載完成檔。
每日上限 **10,000 筆**。

---

## 上傳設定（每次都一樣）

addrCompare 介面 → 逐檔上傳，設定：

- 座標系：**WGS84 經緯度（EPSG:4326）**
- 分單/雙號比對、誤差不限、**僅回傳一筆**、其餘不勾

---

## 標準流程

```bash
# Phase 1：產出待上傳切片
python -m lvr_pipeline.run [batch]
# → data/work/tgos_input/{cat}_input_N.csv  (UTF-8-sig, 5 欄 id,Address,Response_Address,Response_X,Response_Y;Response 留空, ≤10k/片)
# → data/work/tgos_input/{cat}_excluded.csv (補字後仍缺字，供人工)
```

**人工節點**：

1. 把 `data/work/tgos_input/{cat}_input_N.csv` 逐片上傳 addrCompare
2. 等 email 通知後下載完成檔
3. 把完成檔（任意 CSV 名）存入 **`data/work/tgos_done/`**
   - 完成檔必須含欄位：`Address`, `Response_Address`, `Response_X`, `Response_Y`

```bash
# Phase 2：匯入 TGOS 結果 + 生成 NDJSON
python -m lvr_pipeline.run [batch] --from-tgos --supabase
```

---

## 殘餘補里（raw TGOS 也定不到時）

跑完標準流程後仍未定位的殘餘（`unlocated.csv` 還有的棟），多半是偏僻巷弄/地名
**只缺「里」**——raw 送 TGOS 會「搜尋不到」，補上里後就命中（實證見計畫
`docs/plans/web-lilin-enrichment.md`）。

```bash
# 只對小量殘餘補里（zip5 即時查，限速；大量會被擋，務必先跑完標準 TGOS 輪）
python -m lvr_pipeline.2b_enrich_residual --limit 300
# → data/work/tgos_input/residual_enriched_N.csv  (UTF-8-sig, id=building_key, Address=已補里)
# → data/work/residual_enriched_map.csv            (對照 + 補了哪個里)
```

**人工節點**（同標準流程，差別只在送的是補里後地址）：

1. 把 `residual_enriched_N.csv` 上傳 addrCompare（**批次=大量主線**）；
   極少量也可用 TGOS 圖台 UI 逐筆查。送法/紅線見 [`tgos-access-methods.md`](./tgos-access-methods.md)。
   - ⚠️ **不可重用圖台網頁內嵌的 APIKEY**；要即時自動化請自行申請 TGOS 即時金鑰。
2. 下載完成檔 → `data/work/tgos_done/` → 跑 `python -m lvr_pipeline.3_import_tgos`
   - `3_import_tgos` 以**完成檔 Address 欄重算 building_key**；補里只插在區與路之間、
     里會被 building_key 剝除 → 算回**原 building_key**，故正確回灌、無需改回灌邏輯。

`2b_enrich_residual` 跑完會印「殘餘 N / 補到里 X / 無里 Y」當命中量測。

---

## 每日配額管理

- 每日上限 10,000 筆；每片 ≤10,000 行
- 未定位棟（快取 miss）自動分片；隔天繼續上傳下一片
- 分多天上傳沒關係：每次 Phase 2 只匯入已下載的 tgos_done/ 內容
  下次 Phase 1 重跑會自動跳過已快取的棟，只產出剩餘未定位片

---

## 完成檔格式（TGOS 回傳）

| 欄位 | 說明 |
|------|------|
| `id` | 送入的 building_key（3_import_tgos 不用這欄；以 Address 重算 bk） |
| `Address` | 送入的地址（`3_import_tgos` 用此欄計算 building_key） |
| `Response_Address` | TGOS 標準化後地址 |
| `Response_X` | WGS84 經度（lng）—— 有時 X/Y 顛倒，系統自動偵測 |
| `Response_Y` | WGS84 緯度（lat） |

> 若 Response_Address = "找不到指定的門牌地址。" → `3_import_tgos` 自動跳過

---

## 造字排除清單 (`*_excluded.csv`)

registry 無正字規則的造字（Unicode 私用區 U+E000-U+F8FF）或含字面 `?` 的地址補字後仍缺字，
不送 TGOS，改由人工處理：
- 看 `data/work/tgos_input/{cat}_excluded.csv`
- 確認正字後填入 `data/registry/garbled_override.csv`（building_key → 正確地址）
- 重跑 Phase 1，此棟會改走 override 路徑

---

## tx_yyyymm 說明

每筆以**交易年月**（`tx_yyyymm`）索引，非登入批次季別。
格式：`(民國年 + 1911) × 100 + 月份`，例如民國 114 年 11 月 = 202511。
前端依此欄篩選月份範圍，跨多個登入批次的資料自動合併。

`src_batch` 欄位記錄登入批次標籤（如 `115q1`），供追蹤哪批上傳了哪筆。

---

*歷史附記：舊 per-quarter 腳本（`1_prepare_for_tgos.py`、`5_split_no_coordinate.py`、
`6_load_to_supabase.py` 等）已退役，git 歷史保留。*
