# TGOS addrCompare 操作手冊

本文件記錄 TGOS 人工上傳、回傳檔放置、匯入、驗證與回補步驟。所有命令都從 `C:/Code/taiwan-lvr-geodata` 執行。

## 第一批目前狀態

批次 `p3-tgos-20261005-001` 已於 2026-10-05 送出，共 4,871 筆。回傳檔已於 2026-10-06 匯入並驗證。持久狀態位於 `data/work/tgos-state/snapshots/p3-tgos-20261005-001-imported`。匯入結果為 4,428 筆成功、390 筆查無座標、53 筆因回傳不是完整門牌而拒絕。整體地址狀態為 located 53,310、conflict 2,269、unmatched 443。

## addrCompare 上傳設定

上傳 `data/tgos/BATCH_ID/addresses.csv`。不要上傳 `manifest.json`。

座標系選擇 **WGS84 經緯度（EPSG:4326）**。

在 **「模糊比對規則設定」** 區塊套用下列設定：

- 開啟「分單／雙號比對」。
- 誤差選擇「不限」。
- 回傳筆數選擇「僅回傳一筆」。
- 其餘選項不勾選。

## TGOS 回傳後放置檔案

收到 TGOS 完成通知後，下載完成的 CSV。第一批固定放在：

```text
C:/Code/taiwan-lvr-geodata/data/tgos/p3-tgos-20261005-001/response.csv
```

不要覆蓋同一目錄內的 `addresses.csv` 或 `manifest.json`。回傳 CSV 必須保留 `Address`、`Response_Address`、`Response_X`、`Response_Y` 欄位。`id` 欄可以保留，但匯入程式不依賴它。不要用會改變欄名、編碼或前導零的方式另存檔案。

## 匯入第一批回傳

確認 `response.csv` 已放到指定位置後，在 PowerShell 執行：

```powershell
.venv\Scripts\python.exe -m lvr_pipeline import-tgos `
  --state data\work\tgos-state\snapshots\p3-tgos-20261005-001-submitted `
  --batch p3-tgos-20261005-001 `
  --response data\tgos\p3-tgos-20261005-001\response.csv `
  --work-dir data\work `
  --run-id p3-tgos-20261005-001-imported
```

匯入成功後，新的狀態位於：

```text
data/work/tgos-state/snapshots/p3-tgos-20261005-001-imported
```

執行驗證：

```powershell
.venv\Scripts\python.exe -m lvr_pipeline verify-tgos-state `
  --input data\work\tgos-state\snapshots\p3-tgos-20261005-001-imported
```

程式會拒絕欄位缺失、`Address` 無法與送出清單建立唯一對應、重複地址、疑似經緯度顛倒及不明座標系的回傳。原始回傳檔與 SHA-256 會保留為匯入證據。

## 回補月檔與年度包

只有匯入與驗證成功後，才執行回補：

```powershell
.venv\Scripts\python.exe -m lvr_pipeline backfill-output `
  --input data\work\converted\snapshots\p1-115q1-accepted `
  --state data\work\tgos-state\snapshots\p3-tgos-20261005-001-imported `
  --previous data\output\p2-115q1-offline-v2 `
  --notices data\sources\p2_notice.json `
  --output-dir data\output `
  --report data\work\p3-tgos-20261005-001-backfill-report.json `
  --run-id p3-115q1-tgos-001-backfill
```

回補候選位於 `data/output/p3-115q1-tgos-001-backfill`。接著執行：

```powershell
.venv\Scripts\python.exe -m lvr_pipeline verify-output `
  --input data\output\p3-115q1-tgos-001-backfill
```

回補只重建受影響的交易月與對應年度包。驗證完成不等於已公開發布。發布仍需核對來源權利、附件雜湊與 Git parent。

第一批真實回補於 2026-10-06 首次執行時停止。原因是 `package_output` 有 legacy 座標閘門，而 `data/sources/p2_notice.json` 設定 `legacy_coordinates_authorized=false`。專案擁有者當天決定不要求上游權威證明，閘門已移除。

重跑時改用 `data/sources/p3_notice.json`，它列出 TGOS 與地址 repo 的來源與限制。`p2_notice.json` 維持不變，因為它描述已發布的 P2 首版。重跑命令即本節的 `backfill-output`，只需把 `--notices` 換成 `data\sources\p3_notice.json`。候選 `p3-115q1-tgos-001-backfill` 已產生並通過 `verify-output`，尚未發布。

已驗證的 TGOS 結果可先依[地址 patch 操作手冊](address-patch-runbook.md)產生內部 patch。這不等於授權公開月／年資料，也不會自動修改地址 repo。

## 操作狀態

- 上傳成功後，將批次記為 `submitted`。
- 無法確認是否送出時，記為 `submission_unknown`，不可重送。
- 只有上傳前取消才可記為 `cancelled` 並釋放保留。
- 收到回傳前，不建立相同地址的新批次。
