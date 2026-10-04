# 🔒 輸出契約（與前端 mini-taiwan-pulse 的接縫）

本檔定義 `taiwan-lvr-geojson` 產出、由前端 `mini-taiwan-pulse` 消費的格式。
**任一邊要改，先改這份文件並同步另一邊。**

> **唯一真相**：完整契約見
> [`docs/plans/lvr-data-render-split-plan.md`](./docs/plans/lvr-data-render-split-plan.md) §3
> （兩個 repo 各持一份逐字相同的 plan）。本檔為其摘要與快速索引。

---

## 0. 架構（2026-06 重構後）

資料層**只產純點位**、推進 **Supabase**；渲染（色階／柱體／jitter）全在前端動態算。

```
taiwan-lvr-geojson（本 repo）── load 腳本 upsert ──▶ Supabase lvr_points
                                                          ▲ get_lvr_points / get_lvr_months RPC
mini-taiwan-pulse（前端）── 按 bbox + 年月區間 + 類別動態查 ──┘
```

接縫**不再是靜態 geojson 檔**，而是 **Supabase DB schema + 兩支 RPC**。

## 1. Canonical 點位記錄（資料層輸出 / DB 儲存）

中間產物：**NDJSON**（`data/output/canonical/<quarter>_<category>.ndjson`，一行一筆）。
**不含任何渲染欄位**（無 `render_color`、無多邊形、無 jitter）。欄位由 `lvr_pipeline/canonical.py` 產出：

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | string | `編號`，跨批次去重；複合主鍵 `(category, id)` |
| `lng` / `lat` | number | WGS84 |
| `category` | string | `sales` / `rent` / `presale` |
| `tx_yyyymm` | int | 交易/租賃年月（分區鍵），如 `202511` |
| `src_batch` | string | 登入批次，如 `2026q1`，僅供追溯 |
| `total_price` | number | 總價/總額（元） |
| `unit_price` | number | 每坪單價（元）= `單價元平方公尺 × 3.305785` |
| `area_sqm` | number | 建物面積（平方公尺） |
| `building_type` | string | `建物型態` |
| `address` | string | 門牌地址 |
| `tx_date` | string | 原始民國日 7 碼（保留供細查） |
| `props` | object | 其餘 meta 白名單欄位（~12 欄，依類別取，見 plan §3.1） |

## 2. Supabase Schema 與 RPC

DDL / RPC 定義於 [`supabase/migrations/20260611000000_lvr_points.sql`](./supabase/migrations/20260611000000_lvr_points.sql)：

- 表 `lvr_points`：複合 PK `(category, id)`、`geom` generated column、GIST + `(category, tx_yyyymm)` 索引。
- `get_lvr_points(min_lng, min_lat, max_lng, max_lat, ym_from, ym_to, p_category)`
  → 回傳 bbox ∩ 年月區間 ∩ 類別的點，**server-side `limit 20000` 護欄**。
- `get_lvr_months(p_category default null)` → 現有 distinct `tx_yyyymm`（升序），供前端區間 UI 推導預設值。

> **參數名以 SQL 為準**：`get_lvr_points` 的類別參數是 `p_category`（非 `category`），前端 RPC 呼叫須對齊。

## 3. 前端取得方式

前端透過 supabase-js 呼叫上述 RPC（anon key，唯讀）。參考實作：

- `mini-taiwan-pulse/src/data/realEstateLoader.ts`（RPC 呼叫 + 型別）
- `mini-taiwan-pulse/src/map/realEstateDeckLayer.ts`（deck.gl ColumnLayer + 動態色階 + 同址散開）
- `mini-taiwan-pulse/src/hooks/useRealEstatePoints.ts`（bbox/區間變動 → 重抓）

---

## 附錄：舊靜態 geojson 契約（已退場，留存備查）

2026-06 重構前的耦合版：資料層直接輸出烤死渲染的 `{quarter}_rs_{type}.geojson`
（含 `render_color`、六角/圓柱多邊形、jitter）+ `manifest.json`，前端整包靜態 fetch。
**此路徑已由上述動態 RPC 取代，不再維護。** 渲染邏輯保存於
`lvr_pipeline/_legacy_render.py` 供前端移植色階/幾何時對照。
