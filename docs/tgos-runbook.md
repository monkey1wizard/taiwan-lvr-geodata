# TGOS addrCompare 操作手冊

本文件記錄 TGOS 人工上傳、回傳檔放置、匯入、驗證與回補步驟。所有命令都從 `C:/Code/taiwan-lvr-geodata` 執行。

## 第一批目前狀態

批次 `p3-tgos-20261005-001` 已於 2026-10-05 送出，共 4,871 筆。持久狀態位於 `data/work/tgos-state/snapshots/p3-tgos-20261005-001-submitted`。目前等待 TGOS 完成通知與回傳 CSV。

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

## 操作狀態

- 上傳成功後，將批次記為 `submitted`。
- 無法確認是否送出時，記為 `submission_unknown`，不可重送。
- 只有上傳前取消才可記為 `cancelled` 並釋放保留。
- 收到回傳前，不建立相同地址的新批次。
