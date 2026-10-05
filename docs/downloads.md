# 下載月檔與年度包

公開版本以 `data/releases/latest.json` 與各版本同名 JSON 指標為入口。發布前指標不存在時，表示尚未完成公開交付。指標保存不可變 Release 的 manifest URL 與 SHA-256。

選定交易月份 `tx_yyyymm`，再選 sales、presale 或 rent，以及 GeoParquet、GeoJSON 或 NDJSON。`YYYYMM_category` 檔名沿用舊規格。單月下載不需要年度 ZIP、raw 或整份維護狀態。

```bash
uv run --locked --python 3.13.16 python -m lvr_pipeline fetch-output --manifest-url MANIFEST_URL --manifest-sha256 MANIFEST_SHA256 --target data/downloads/my-month --month 202601 --category sales --format geoparquet
```

將指標中的值代入 `MANIFEST_URL` 與 `MANIFEST_SHA256`。目標必須是不存在的目錄。程式核對 manifest 雜湊、版本與選定附件的大小／雜湊，完成後才建立目標目錄。省略 `--format` 會取得該月／類別三格式，另核對記錄對應。

年度 ZIP 依格式提供，直接包含原月檔。年度 manifest 列出月份、缺失月份及可能的多片 ZIP。下載所有該年度／格式的片才能取得宣告年度範圍。月或年標示 `scope_limited` 時，只涵蓋指定來源批次，不能當成完整市場資料。

沒有定位的記錄仍存在，geometry 為 null。Polygon 的 `is_approximation=true` 表示多門牌定位點的 bbox，不能作地籍或建物輪廓使用。資料粒度是來源觀測，`transaction_key=null` 不能當作已完成交易去重。

來源、顯名及使用限制見每版 `NOTICE.json`。操作與驗收範圍見 [P2 文件](p2-offline-output.md)。
