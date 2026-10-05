# Cloud agent：從離線快照接續 P3

P3 已提供 TGOS 批次保留、人工狀態確認、嚴格回傳匯入及歷史回補命令。資料工作可在本機或 cloud 執行，但所有 agent 必須接續同一份最新 `tgos-state`。TGOS 憑證、人工上傳與下載留在 Git 外。

在 Linux repo 根目錄執行 `bash scripts/setup.sh`。先安裝固定 uv 版本 0.12.23。安裝與測試只使用合成 fixtures，不下載真實資料，也不需要憑證、GAL、`.dev` 或 raw ZIP。

## 取得已發布的離線狀態

從 `data/releases/latest.json` 選取 manifest URL 與 SHA-256：

```bash
uv run --locked --python 3.13.16 python -m lvr_pipeline fetch-output \
  --manifest-url MANIFEST_URL --manifest-sha256 MANIFEST_SHA256 \
  --target data/downloads/offline --maintenance
```

維護包包含 `handoff.json`、P1 已轉換觀測、地址狀態、證據、成員關聯、未定位池與 TGOS 狀態。程式驗證 manifest、Parquet 結構與關聯，不需要 raw。使用 `handoff.json` 的 `converted`、`state` 與 `state_stage` 相對路徑找輸入。P2 的 `state_stage` 為 `offline-state`；P3 回補版為 `tgos-state`。

## 使用最新離線地址資料

先取得乾淨的 `taiwan-address-data` checkout，再固定來源描述。本專案不能修改或發布地址 repo。

```bash
git -C ../taiwan-address-data pull --ff-only
uv run --locked --python 3.13.16 python -m lvr_pipeline pin-address-source \
  --address-dir ../taiwan-address-data \
  --output data/sources/address_source.json
```

接著依 P2 命令重建離線索引、地址池及 `offline-state`。2026-10-05 使用地址 commit `02887978ef19c1067e339787bae976a72d4723af` 的實測結果為 56,022 個地址鍵：48,882 個 located、2,269 個 conflict、4,871 個 unmatched、0 個 outside_scope。只有 unmatched 與 outside_scope 能成為 TGOS 候選；conflict 必須人工覆核。

這份地址來源的再散布權利仍是 `pending_upstream_evidence`。它可供內部定位與候選縮減，但不能僅因程式成功就公開發布來源座標。

## 建立一輪 TGOS 批次

操作員先確認同一 TGOS 共用帳號在服務日期已由其他工作使用的筆數，填入 `EXTERNAL_USED`。不要將未確認值填成 0，也不要為未來日期預建批次。

```bash
uv run --locked --python 3.13.16 python -m lvr_pipeline prepare-tgos \
  --state STATE_PATH --service-date YYYY-MM-DD \
  --external-used EXTERNAL_USED --work-dir data/work \
  --exchange-dir data/tgos --run-id RUN_ID
```

成功後先產生不可變 `tgos-state`，再交付 `data/tgos/BATCH_ID/addresses.csv` 與 `manifest.json`。CSV 為 UTF-8-sig，標頭必須完全是 `id,Address,Response_Address,Response_X,Response_Y`，後三欄在上傳前留空。單片與單日不超過 10,000 筆。候選少於剩餘配額時只輸出實際候選，不建立空批次。

相同查詢不會自動重送。操作員核准重試 failed、rejected 或 cancelled 查詢時，從前次 manifest 取得 fingerprint，另加 `--retry-query-fingerprint FINGERPRINT --retry-reason REASON`。程式會記錄前次批次；沒有原因、查無歷史或狀態不可重試時拒絕建立批次。conflict 留在人工覆核，不送 TGOS 重試。

### addrCompare 上傳設定

座標系選擇 **WGS84 經緯度（EPSG:4326）**。

在 **「模糊比對規則設定」** 區塊套用下列設定：

- 開啟「分單／雙號比對」。
- 誤差選擇「不限」。
- 回傳筆數選擇「僅回傳一筆」。
- 其餘選項不勾選。

上傳後，依實際結果記錄狀態：

```bash
uv run --locked --python 3.13.16 python -m lvr_pipeline set-tgos-status \
  --state TGOS_STATE_PATH --batch BATCH_ID --status submitted \
  --work-dir data/work
```

`--status` 可用 `submitted`、`submission_unknown` 或 `cancelled`。只有尚未上傳的 prepared 批次取消時會釋放保留。可能已提交的批次仍占用當日配額；提交不明不能因逾時自動釋放。

## 匯入回傳並回補 output

完整人工步驟與第一批實際命令見 [TGOS addrCompare 操作手冊](tgos-runbook.md)。

TGOS 回傳後，將完成的 CSV 放在 `data/tgos/BATCH_ID/response.csv`。不要覆蓋 `addresses.csv` 或 `manifest.json`。回傳檔必須包含 `Address`、`Response_Address`、`Response_X`、`Response_Y`。`Address` 必須與原批次一對一且集合完全相同。程式不要求 TGOS 回傳 id，也不猜座標軸或 CRS。

```bash
uv run --locked --python 3.13.16 python -m lvr_pipeline import-tgos \
  --state TGOS_STATE_PATH --batch BATCH_ID \
  --response data/tgos/BATCH_ID/response.csv \
  --work-dir data/work --run-id IMPORT_RUN_ID

uv run --locked --python 3.13.16 python -m lvr_pipeline verify-tgos-state \
  --input IMPORTED_STATE_PATH
```

相同批次與回傳雜湊可重複執行而不增生資料。查無、拒絕與衝突也會保存。有效結果先成為地址觀測，再重建索引；座標不同時保留衝突。

```bash
uv run --locked --python 3.13.16 python -m lvr_pipeline backfill-output \
  --input CONVERTED_PATH --state IMPORTED_STATE_PATH \
  --previous PREVIOUS_OUTPUT_PATH --notices NOTICE.json \
  --output-dir data/output --report data/work/BACKFILL_REPORT.json \
  --run-id OUTPUT_RUN_ID
```

報告列出變更月份、年份及未變月檔數，前版必須仍可讀。發布前仍要通過來源權利、附件雜湊、預期 Git parent 與公開索引檢查。遇到衝突時保留來源觀測，不推定最近點、第一列、相同路名、相似 Response_Address 或舊 key 代表同一門牌。
