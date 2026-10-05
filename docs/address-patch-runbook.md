# 地址 patch 操作手冊

本文件說明如何把已驗證的 TGOS 門牌結果轉成供 `taiwan-address-data` 審查的 patch。所有命令都從 `C:/Code/taiwan-lvr-geodata` 執行。本步驟只建立內部快照，不會修改或發布地址 repo。

## 第一批結果

第一批正式快照位於：

```text
data/work/address-patch/snapshots/p4-tgos-20261005-001-patch-v3
```

快照包含：

- `address_patch.parquet`：4,264 個可匯入門牌，包含 patch 識別、`building_key_v2`、地址 repo 的 14 欄相容資料及 WGS84 經緯度。
- `provenance.parquet`：4,301 筆來源 sidecar。每筆連回 TGOS 批次、查詢、回傳列、回傳檔 SHA-256 及證據 ID。
- `quarantine.parquet`：127 筆隔離結果。這些結果無法從固定的行政區檔取得唯一村里編碼，未寫入 patch。
- `quality.json`、`manifest.json`：輸入、程式、行政區檔、產物雜湊、筆數及資源紀錄。

多筆 TGOS 查詢可能指向相同完整門牌。`address_patch.parquet` 每個目標門牌只保留一列，`provenance.parquet` 保留所有來源，因此來源筆數可以高於 patch 筆數。

## 產生 patch

使用匯入後的 TGOS 狀態。行政區對照則使用固定地址 repo 的全部行政區檔：

```powershell
.venv\Scripts\python.exe -m lvr_pipeline export-address-patch `
  --state data\work\tgos-state\snapshots\p3-tgos-20261005-001-imported `
  --area-file C:\Code\taiwan-address-data\area_1984.csv `
  --area-file C:\Code\taiwan-address-data\area_2010.csv `
  --area-file C:\Code\taiwan-address-data\area_2014.csv `
  --area-file C:\Code\taiwan-address-data\area_2015.csv `
  --area-file C:\Code\taiwan-address-data\area_custom.csv `
  --work-dir data\work `
  --run-id p4-tgos-20261005-001-patch-v3
```

所有行政區檔都會寫入 binding。舊版與新版檔案若對同一名稱提供不同編碼，程式只接受與該地址鄉鎮編碼一致且唯一的村里編碼。

## 驗證 patch

```powershell
.venv\Scripts\python.exe -m lvr_pipeline verify-address-patch `
  --input data\work\address-patch\snapshots\p4-tgos-20261005-001-patch-v3
```

預期結果：

```json
{"verified": true, "snapshot_id": "p4-tgos-20261005-001-patch-v3", "patch_rows": 4264, "quarantine_rows": 127}
```

驗證會檢查 Parquet schema、主鍵、patch 與來源 sidecar 的關聯、村里證據及座標範圍。TGOS 的 `N號之M` 表示會轉成地址 repo 使用的 `N之M號`，原始回傳地址仍保留在 sidecar。程式不猜測缺少的行政區編碼，也不把隔離列寫入 patch。

## 下一步

T-19 需要在 `C:/Code/taiwan-address-data` 實作獨立 `supplements/` 層、可重複匯入及固定 14 欄輸出。執行 T-19 前，先以本快照的 `address_patch.parquet`、`provenance.parquet` 及 `manifest.json` 作為固定輸入。不要直接把 Parquet 複製成 `roads/*.csv`，也不要將 127 筆隔離結果人工補成未知村里編碼。
