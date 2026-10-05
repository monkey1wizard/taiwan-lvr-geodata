# Cloud agent：從離線快照接續

P2 支援下載、驗證與重產離線 output。P3 的 TGOS 準備／匯入及歷史回補尚待實作，不能將 P2 的未定位池直接發布成 TGOS 批次。

在 Linux repo 根目錄執行 `bash scripts/setup.sh`。先安裝固定 uv 版本 0.12.23。安裝只建置合成 fixtures 與測試，不下載真實資料，也不需要憑證、GAL、.dev 或 raw。

從已提交的 `data/releases/latest.json` 選取 manifest URL 與 SHA-256，執行：

```bash
uv run --locked --python 3.13.16 python -m lvr_pipeline fetch-output --manifest-url MANIFEST_URL --manifest-sha256 MANIFEST_SHA256 --target data/downloads/offline --maintenance
```

維護包包含 `handoff.json`、P1 已轉換觀測與原始欄位／排除／診斷關聯，以及 P2 地址狀態、證據、成員關聯、未定位池與空 TGOS 帳本。程式驗證所有內部 manifest、Parquet 結構與關聯，不需要 raw。若缺少必要狀態，程式明確失敗；取得相容快照仍不可補足資料時，才要求 raw 重建。

使用 `handoff.json` 的 `converted` 與 `state` 相對路徑，從 `data/downloads/offline/maintenance/` 找到輸入。將這兩個路徑代入 `package-output --input ... --state ...`，並以維護包的 `NOTICE.json` 作 `--notices`。使用新的 output 目錄與 run ID，避免覆寫既有版本。

狀態的 `tgos_started` 初始為 false。它不能作為重設配額的選項。TGOS 開始後，需要 P3 的持久帳本與保留規則，P2 命令會拒絕以離線流程覆寫那些狀態。

遇到衝突時，保留來源觀測供審查。不要推定最近點、第一列、相同路名或舊 key 代表同一門牌。不要修改相關地址 repo，地址回饋另依 P4 任務執行。
