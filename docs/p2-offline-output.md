# P2：離線定位與月／年資料交付

## 範圍與狀態

P2 實作 T-08～T-13、T-22、T-23。所有程式在本機正式目錄的 main 修改、測試及提交。階段驗收以本文件的命令、測試結果及公開快照為準，不能由程式存在推定通過。

目前正在驗證首版真實範圍：P1 已驗收的 `p1-115q1-accepted`，共 102,743 筆來源觀測、102,881 個地址成員。交易月份共有 87 個，從 201712 到 202610。原始批次只有 115q1，因此各月及年度都是 `scope_limited`。沒有出現的月份是 `absent`，不能視為全月沒有交易。

離線行政區與候選路名使用固定 `taiwan-address-data` commit `752c87d36a8e52d9b71680115c1c19d1a6d3e4ec`。首版可公開座標使用另行固定的官方臺北市門牌 CSV。舊地址 repo 的座標來源權利仍未完全證明，因此不使用或發布那些座標。其餘縣市保留 `outside_scope` 或地址診斷。

## 來源與坐標條件

官方輸入描述在 [official_taipei.json](../data/sources/official_taipei.json)。CSV 有 124,662,109 bytes，SHA-256 為 `7212058161f10a13d52ee46e74155526ca4ebf182b151562ba0e533f882a89ab`。來源為[臺北市門牌位置資訊](https://data.gov.tw/dataset/155472)，適用[政府資料開放授權條款第1版](https://data.gov.tw/license)。原 CSV 留在 Git 外，維護快照只保留本次地址池所需的定位證據及來源關聯。

輸入座標系統明確指定為 EPSG:3826。這是既有地址更新器對該來源的設定，並參照臺北市政府的 TWD97 平面座標文件。程式依指定的橫座標、縱座標順序轉為經度、緯度，不以數值範圍選擇座標系統或交換軸。官方八位鄉鎮碼轉七位 DGBAS 碼也使用明確的直轄市來源規格，並核對固定行政區表。

[NOTICE](../data/sources/p2_notice.json) 記錄提供者、來源版本、授權、顯名及限制。P1 本機 raw 的歷史取得時間與原下載 URI 未提供，首版只宣告已固定的輸入雜湊，不宣稱重新下載了相同官方檔案。

## 已實作命令

從 repo 根目錄執行。下列資料建置命令需要自行提供已固定的地址 checkout 與官方 CSV，不屬於安裝或測試。安裝及合成測試不會下載真實資料。

```powershell
uv run --locked --python 3.13.16 python -m lvr_pipeline build-offline-index --address-dir ../taiwan-address-data --county 63 --official-source data/sources/official_taipei.json --official-file data/cache/official-source/taipei-address.csv --run-id p2-taipei-official
uv run --locked --python 3.13.16 python -m lvr_pipeline build-address-pool --input data/work/converted/snapshots/p1-115q1-accepted --address-dir ../taiwan-address-data --index data/work/offline-index/snapshots/p2-taipei-official --run-id p2-115q1-pool
uv run --locked --python 3.13.16 python -m lvr_pipeline resolve-offline --pool data/work/address-pool/snapshots/p2-115q1-pool --index data/work/offline-index/snapshots/p2-taipei-official --run-id p2-115q1-state
uv run --locked --python 3.13.16 python -m lvr_pipeline verify-offline-state --input data/work/offline-state/snapshots/p2-115q1-state
uv run --locked --python 3.13.16 python -m lvr_pipeline package-output --input data/work/converted/snapshots/p1-115q1-accepted --state data/work/offline-state/snapshots/p2-115q1-state --notices data/sources/p2_notice.json --run-id p2-115q1-offline
uv run --locked --python 3.13.16 python -m lvr_pipeline verify-output --input data/output/p2-115q1-offline
```

首次 `resolve-offline` 不需要 `--prior-state`。後續指定舊狀態時，程式仍從新索引重新計算，不承接已移除的座標證據。若 TGOS 已開始，P2 命令拒絕重設狀態，須等待 P3 帳本整合。

## 資料契約

索引保存來源列觀測，包括重複、無效及衝突。行政區依 CSV 欄位與固定代碼表核對，檔名只作選定輸入範圍用途。代碼保持字串與前導零。別名只有行政區表的明確同碼表示，沒有距離或子門牌等價推論。

`unique_addresses.parquet` 每個 v2 鍵一列。`address_occurrences.parquet` 每個來源地址成員一列，獨立保存原鍵及套用原因，不把大量出現關聯塞入單一地址列。代表地址使用固定最小表示，排程家族不作識別合併。亂碼路名只有唯一候選且完整門牌精確命中唯一座標時才套用。

狀態包含 `address_index`、`address_observations`、`unique_addresses`、`address_occurrences`、`unmatched_addresses`、空的 `verified_aliases` 及空的 `tgos_results`。`tgos_started=false`。不同有效座標是 `conflict`，不任取第一列；沒有有效證據是 `unmatched` 或 `outside_scope`。不能將衝突池直接當作可送 TGOS 的池，P3 還須套用查詢資格。

GIS 以 `source_observation` 為粒度，沒有已證明交易識別時，`transaction_key` 保持 null。金額保留在觀測列一次。三格式使用相同 ID、屬性、幾何與筆數。

| 定位結果 | 幾何 |
| --- | --- |
| 沒有定位點 | null |
| 一個唯一點 | Point |
| 買賣／預售有至少兩點，bbox 寬高皆正 | Polygon，`is_approximation=true` |
| 買賣／預售 bbox 同軸退化 | MultiPoint |
| 租賃至少兩個唯一點 | MultiPoint |
| 部分成員定位 | 保留可用幾何與 `location_status=partial` |

GeoParquet 固定 1.1.0、CRS84、WKB，幾何不是地籍邊界。GeoJSON 與 NDJSON 保存相同 Feature。Parquet 與文字輸出分批寫入，每批最多 1,024 列或 8 MiB。單筆來源觀測超過 10,000 個成員時明確拒絕，不以無界聚合消耗記憶體。

## 月檔、年包與資源

月檔位於 `monthly/YYYY/YYYYMM/YYYYMM_category.{parquet,geojson,ndjson}`，月 manifest 保存各類別筆數、null／部分定位、近似幾何及金額核對。宣告範圍中沒有該類別時，產生可讀的空檔並標示 `empty_in_scope`。

年度 ZIP 依格式打包同一版本的月檔原始位元組。年度 manifest 列出實際月份與缺失月份，不能宣稱完整曆年。超過設定預算時按完整月／類別檔分年度 ZIP 片，所有片列入年度 manifest。若單一月檔或維護包已超過附件預算，程式明確停止並要求新增分片契約，不強行提交 Git。

`size_report.json` 保存實際月檔、年度包、維護包及附件大小，另記錄記憶體峰值、產生時間、實際保留暫存與保守磁碟預算。磁碟預算不是實測工作磁碟峰值，兩者分開記錄。Git 只保存程式、契約、合成測試、來源描述及小型發布指標，生成資料的 Git 歷史成長為零。

## 驗收紀錄

| Tasks／test points | 驗證 | 結果 |
| --- | --- | --- |
| T-10～T-12／TP-11～TP-13 | 合成行政區、前導零、來源欄位、重複／衝突、來源移除與亂碼精確證據 | pass |
| T-13／TP-14、TP-26 | 幾何 WKB、跨年交易月、三格式／金額對應、部分／null 幾何、年度分片原檔雜湊 | pass |
| T-09／TP-09、TP-25 | 合成快照在空目錄下載維護狀態，不提供 raw | pass，GitHub Linux 真實交接 not-run |
| T-22／TP-23 | 模擬上傳中斷、附件雜湊錯誤、過時 parent 與正確提交順序 | pass，真實公開傳送 not-run |
| T-08／TP-08 | 真實 output 大小與配置 | not-run |
| T-23／TP-24 | 真實公開月／年與狀態核對 | not-run |

完整命令與最終快照證據將在實測後補入本文件與 `p2-evidence.json`。以上不等同 P2 全部驗收完成。
