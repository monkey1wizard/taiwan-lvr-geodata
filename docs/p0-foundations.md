# P0 實作與驗收紀錄

日期：2026-10-05，Asia/Taipei。基準 commit：`9fec3244785ac48c295e4b69be82f1011789851f`。

本次實作位於工作副本，已 commit 並 push 至 codex/phase-0-foundations，見[草稿 PR #1](https://github.com/monkey1wizard/taiwan-lvr-geodata/pull/1)。`C:/Code` 內的原 repo 仍維持上述基準，沒有修改。寫入權限請求未取得授權，因此使用可寫入工作副本完成雲端交付。PR 尚未合併。

## 任務結果

| Task | 已實作與實測 | 尚待驗收 |
| --- | --- | --- |
| T-01 | AGENTS、固定 Python／uv／套件、Linux setup、無憑證 CI、合成三類別 ZIP／空批次／14 欄地址樣本。Windows 與 GitHub Ubuntu 的固定 Python 3.13.16 環境均通過 145 個測試。 | TP-01 已在 GitHub Ubuntu 驗證，尚待 PR 審查／合併。 |
| T-02 | 58 個 raw ZIP 的大小、SHA-256、CRC、成員、交易欄名及類別／縣市字首清單。固定地址 commit 的 27,175 個路檔、行政區檔及 road.csv 雜湊。TP-02 的缺失、損壞、雜湊變動與已知空批次樣本通過。 | 直接下載 URI、原始取得時間及當期上游授權證據未確認，欄位明確保留待確認。沒有上傳 raw 或地址資料列。 |
| T-03 | 四種版本化 JSON Schema、來源觀測／成員識別、關聯檢查、固定日期截止與嚴格曆日驗證、v2 地址鍵。TP-03／TP-04 合成測試通過。 | 尚未證明來源交易識別或修訂順序，transaction_key 固定空值，不做合併。全歷史正規化驗收屬 P1／P5。 |
| T-04 | 本機不可變版本、雜湊／筆數／JSONL 結構與關聯驗證、parent 保護、獨占發布鎖、指標切換及中斷重試。TP-10 本機測試通過。 | 遠端 CAS／Release 屬 T-22。Linux 樣本測試已通過。網路檔案系統／電源中斷耐久性未驗證。 |

P0 實作與本階段測試已完成。GitHub Ubuntu 的[固定安裝與合成測試](https://github.com/monkey1wizard/taiwan-lvr-geodata/actions/runs/37259318617)通過，程式版本為 ca0297578ef623795845f89c8d17a99ad69274f0。PR 保留為草稿，供審查後合併；後續真實資料與資源驗收仍依各 task 執行。

## 可執行命令

Linux 安裝前需有 uv 0.12.23。安裝使用已追蹤的 uv.lock，不下載真實資料、不讀憑證。

```bash
bash scripts/setup.sh
```

Windows 可在專案根目錄執行：

```powershell
uv sync --locked --group dev --python 3.13.16
uv run --locked --python 3.13.16 python scripts/build_fixtures.py
uv run --locked --python 3.13.16 python -m pytest -q
```

實測環境為 Windows、Python 3.13.16、uv 0.12.23。固定套件與所有間接相依套件見 uv.lock。測試結果為 145 passed。既有 94 個測試保留，新增 51 個測試。

Docker Linux 驗證嘗試在容器啟動前失敗：`permission denied while trying to connect to the docker API at npipe:////./pipe/docker_engine`。本機 Docker 結果為 NotRun；隨後 GitHub Ubuntu CI 已成功執行 setup.sh，145 passed in 1.57s。

## 契約與邊界

新程式使用 `building_key_v2()` 與 `validated_roc_to_tx_yyyymm()`。既有 `building_key()` 與 `roc_to_tx_yyyymm()` 的行為保留。v2 保留行政範圍、無道路地名及完整子門牌，只有明訂等價表示共用鍵。連字號、缺字、不明範圍或未證明門牌送覆核。歷史行政區對照尚待後續來源規則，不自動改寫。

觀測金額只存一次。currency 固定 TWD，amount_scale 固定 100，amount_minor 是整數分，原始元金額乘 100 後保存。地址成員沒有金額欄，來源編號不被當成唯一交易鍵。原始值與未知欄位由觀測保留。JSONL 可驗證四種列契約與觀測／成員／排除關聯。CSV 與 Parquet 快照驗證檔案雜湊及筆數，尚不提供完整 Parquet 領域結構／外部索引鍵驗證；P1 的型別化轉換須補齊。

`SnapshotStore` 是 Python API，尚未建立管線 CLI。它只處理本機檔案系統。發布前驗證固定 parent 和全部附件，再切換 `current.json`。錯誤或並行 writer 不覆蓋前版。鎖在程序崩潰後殘留時，需人工核對，不自動刪除。公開 GitHub、TGOS 帳本、GIS、月／年輸出及地址回補仍待 P2～P5。

## 來源清單

`data/sources/raw_manifest.json` 與 `address_source.json` 是固定來源的中繼資料，不含交易列或座標列。raw 的年度使用民國來源批次標記，不是使用者 output 年／月。`101q1` 只有 manifest.csv 與 build.ttt，明確記為已知空批次。

清單是本機檢查結果。raw 沒有不可變公開下載 URI，不能據此宣稱全新 cloud 可取得所有輸入。地址來源標為 legacy_base，不能宣稱全部是官方資料或已確認上游重發布權利。取得時間不使用檔案修改時間代替。

重新盤點時，明確提供原始來源位置及乾淨地址 checkout，不會複製資料列：

```bash
python scripts/inventory_sources.py --raw-dir /inputs/raw --address-repo /inputs/taiwan-address-data --output-dir data/sources
```

## API 依據

Parquet metadata 與讀取方法依 [Apache Arrow 官方 API](https://arrow.apache.org/docs/python/generated/pyarrow.parquet.ParquetFile.html)。DuckDB 的 Python 連線與查詢依 [官方 Python 文件](https://duckdb.org/docs/stable/clients/python/overview)。CI 的指定 uv 版本設定依 [setup-uv v6](https://github.com/astral-sh/setup-uv/tree/v6)。這些文件支持使用方式。Linux 驗收結果另以 GitHub CI 紀錄為準。
