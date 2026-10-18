# B 程式定稿與重產送查檔

類別 i：送出下一批 TGOS 前必須完成。修訂日期：2026-10-18。現況與擁有者決定以 [00 主企劃](00-主企劃.md)與[擁有者決定與驗收總表](擁有者決定與驗收總表.md)為準，本文件只補這一段的工作、規格與陷阱。

## 目的

送出去的查詢文字由門牌鍵產生，門牌鍵由地址解析規則產生。第 1 批結果顯示解析有缺陷，而且全量重跑後「配不到的門牌」集合會變。每送一批花掉一天且不可收回，所以先把會改鍵或文字的修正做完、讓 `run-full` 能帶入帳本、重跑一次得到過渡基線、從過渡基線重產送查檔，然後才開始每日交換。預計 2 到 3 個工作日。

TGOS 採用規則，也就是匯入回傳時怎麼判定同址，不影響送什麼，放在類別 ii 的 [C 子企劃](C-TGOS交換與採用規則.md)。

## 架構決定

- [x] b01 10-18 擁有者裁定四項：第 2 批等本段完成、從新基線重產後才上傳；送查文字保留鄰與村里；帳本快照改為只存帳本表並以雜湊引用來源狀態；第二版在新基線後先發，後到批次走回補
- [x] b02 10-18 寫入總表決定表

## 工作

### B-1 修地址解析缺陷

第 1 批查詢文字中看到的我方問題，修正後這些門牌可能在離線階段就配得到。每條規則依 R07-6b 的方式有獨立正反案例，寫進[地址規則](../地址規則.md)，案例先給擁有者看。

- [x] b03 鄉鎮名重複：`花蓮縣瑞穗鄉瑞穗鄉瑞美村`。比照現行只刪重複縣市名的 `drop_repeated_county`，加刪重複鄉鎮名
- [x] b04 單位字重複：`高雄市前鎮區區瑞昌里`
- [x] b05 縣市與鄉鎮不一致：`苗栗縣苗栗市公館鄉玉谷村`。不自動修，記 `invalid_admin`
- [x] b06 `七之1號` 類解析：目前拆成街路 `國民中路七之` 加號 `1`，16 個鍵因此算不回原鍵
- [x] b07 村里欄混入鄉鎮名或單位字的 61 筆：擁有者尚未裁定修正後重送或記 `invalid_admin`，建議修正後重送，開工時請擁有者確認
- [x] b08 含「樓」字的地名與解析缺口 27 筆，例如 `塔樓路`、`潭底零售市場二樓`、`成功路二一五之六樓之1號`，逐類判定是地名還是缺口
- [x] b09 正反案例寫進地址規則，`NORMALIZATION_VERSION` 依規則變更升版。10-18 完成，升為 `v2.7`，見 [B-1 紀錄](../records/B-1地址解析.md)

### B-2 距離精度

- [x] b10 `haversine_m` 與 `_haversine_sql` 的距離、`math.fsum` 總和都四捨五入到 0.001 m 後儲存與比較；`contracts/validate.py` 以同一函式重算，維持完全相等比對。單元測試含 30.0004 與 30.0006 公尺邊界。Linux 容器逐檔相符的驗收在 E 段第二版的 R08-2a 重做

### B-3 行政區表擴充到村里層級

C 段的村里代碼採用規則要用這張表。表的擴充會改綁定，所以要在重跑前做。

- [x] b11 `config/rules/admin_towns.json` 加入釘選版本 `taiwan-address-data` 的 `area_1984.csv`、`area_2010.csv`、`area_2014.csv` 村里層級列，即 `dgbas_id` 形如 `6801300-006` 的列：村里代碼、名稱、所屬鄉鎮代碼、來源表。重新產生後更新雜湊與 metadata，依 R07-6c 方式重新釘選。離線定位與門牌鍵不變

### B-4 `run-full` 讀帳本

- [x] b12 `run-full` 新增 `--tgos-ledger`，指向 TGOS 狀態快照。必須同紀元；以既有帳本帶入邏輯帶入批次與查詢，已完成批次改回 submitted，再以 `data/tgos/` 中記錄的回傳檔與雜湊重新匯入。回傳檔雜湊不符時拒絕
- [x] b13 新階段 `tgos-state` 在 `offline-state` 之後；`review` 與 `output` 讀它。地址池建置讀帳本中 `tgos_district_fill` 結果補區。10-18 前半完成；補區延後到 00 主企劃 e00，因第 1 批沒有缺區查詢，見 [B-4 紀錄](../records/B-4帳本接入run-full.md)
- [x] b14 帳本快照改為只存帳本表，以雜湊引用來源離線狀態，不整份複製。與 R07-6a 護欄的相容方式先寫進[資料契約](../資料契約.md)再實作。目前每個快照複製 1.1 GB，25 個快照已占 27 GB
- [x] b15 合成 `run-full` 帶帳本與不帶帳本各一次通過

### B-5 過渡基線

這個 run 只用來算「配不到的門牌」集合並重產送查檔，不發布，所以不重做 V-10b 與 R08-2a。那兩項在 E 段第二版的 run 重做。

- [x] b16 全量 `run-full` 帶入第 1 批帳本，新 run ID，DuckDB 上限依總表
- [x] b17 V-12 初判：合成案例全部通過；第 1 批以現行規則重新匯入，採用與拒絕符合規則；寫成檢查腳本，C 段每批重跑
- [x] b18 核對新 run 的 `unmatched` 集合與基線的差異，差異筆數與原因寫進紀錄。新 run 記為過渡基線，更新 00 與總表。10-18 完成，過渡基線 `b16-20261018b`，見 [B-5 紀錄](../records/B-5過渡基線.md)

### B-6 從過渡基線重產送查檔

- [x] b19 取消現有第 2 到 13 批，寫原因
- [x] b20 從過渡基線連續 `prepare-tgos` 至沒有候選，文字不變的已取消查詢以核准重送帶入並記錄理由
- [x] b21 核對：新批次總筆數與過渡基線 `unmatched` 加缺區候選一致、重複鍵與重複文字為 0、第 1 批的鍵未重選、每筆文字算回原鍵、送查檔雜湊等於帳本
- [x] b22 R09-7 紀錄補「第二次重產」節與新舊批次對照表，舊資料夾保留並標為已取消
- [x] b23 更新 C 段的進度表，告訴擁有者可以開始每日上傳

### B-7 收尾

- [ ] b24 全套測試 `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=C:/Code/taiwan-lvr-geodata/data/tmp/b/pytest`
- [ ] b25 每張子卡從[空白範本](../task-result-template.md)複製紀錄到 `docs/records/`
- [ ] b26 同步地址規則、資料契約、README、00 與總表
- [ ] b27 本機提交

## 程式事實與行號

- [x] b28 10-18 以 HEAD `e9be8d7` 查定，haiku 子代理查、Claude 抽查 15 條相符。施做前仍先 `grep` 確認

| 事實 | 位置 |
| --- | --- |
| `DAILY_LIMIT = 10_000`；CLI `--limit` 預設是字面值 10_000，不引用常數 | `lvr_pipeline/tgos/batches.py:20`、`lvr_pipeline/cli.py:97` |
| 送查選擇 `select_unmatched_candidates`，只選 `unmatched` 與 `outside_scope` | `batches.py:306-349`，過濾在 `:327` |
| 文字算不回原鍵的計數 `text_not_round_trip` | `batches.py:337-339` |
| 門牌層級查詢文字 `key_query_text`，往返檢查 `building_key(text) != key` | `batches.py:214-230`，檢查在 `:228-229` |
| 缺區查詢文字 `district_query_text`、候選區與無候選計數 | `batches.py:233`、`:352-425`、報告欄位 `:624` |
| 可重送狀態 `{failed, rejected, cancelled}` | `batches.py:498` |
| 批次狀態轉換表：prepared → submitted／submission_unknown／cancelled；submission_unknown → submitted／cancelled；submitted → cancelled | `batches.py:670-674`，函式 `transition_batch` `:649-700` |
| 帳本帶入 `_carry_ledger` | `batches.py:253-303` |
| TGOS 狀態快照複製來源狀態全部檔案：`_write_state` 迴圈把來源路徑交給 `Stage.finish`，`SnapshotStore.add` 以 `shutil.copyfileobj` 逐檔複製 | `batches.py:109-119`、`lvr_pipeline/storage/runs.py:519-539`、複製在 `runs.py:201` |
| 匯入接受 submitted、submission_unknown、completed；completed 且回傳雜湊相同直接回傳原快照；不同則拒絕 | `lvr_pipeline/tgos/validate.py:242`、`:245`、`:248-249` |
| 距離 `haversine_m`、代表點 `adopt_coordinates` | `lvr_pipeline/results/reconcile.py:121-128`、`:131-163`，唯一呼叫 `:147` |
| SQL 距離 `_haversine_sql` 與兩個呼叫處 | `lvr_pipeline/offline/index.py:361-368`、`index.py:502`、`lvr_pipeline/results/review.py:388` |
| 驗證以 `adopt_coordinates` 重算 `coordinate-resolution` 並完全相等比對 `max_distance_m` | `lvr_pipeline/contracts/validate.py:87-133`，比對在 `:111`、`:128` |
| `RUN_FULL_STAGES`：offline-index、ingested、normalized、converted、address-pool、offline-state、review、output、run-report | `lvr_pipeline/pipeline.py:314-315` |
| `admin_towns.json`：產生 `write_admin_towns`、CLI 預設路徑、載入 `load_admin_towns`、`admin_towns_stale` 檢查與 `run_full` 呼叫 | `lvr_pipeline/addresses/admin.py:146-154`、`cli.py:25`、`admin.py:157-175`、`admin.py:182-192`、`pipeline.py:438` |
| `NORMALIZATION_VERSION = "v2.6"`、`drop_repeated_county`、`building_key`、`door_parts`、`same_door` | `lvr_pipeline/addresses/identity.py:15`、`:66-73`、`:133-184`、`:187-195`、`:212-222` |
| `canonicalize` 在 `AdministrativeNames` 類別內，不在 identity.py | `lvr_pipeline/offline/match.py:62` |
| `normalize_address` | `lvr_pipeline/transactions/normalize.py:39-43` |
| 程式指紋的檔案清單：`lvr_pipeline` 各子目錄 `*.py`、`contracts/json/*.json`、`uv.lock` | `runs.py:485-497` |

## 陷阱

- 本段期間不得上傳現有第 2 批。
- 不得在 b12 之前單獨改距離精度：TGOS 命令複製基線 `coordinate-resolution` 時逐列重驗，先改精度會使讀基線失敗。b10 與 b12 一起進全量重跑。
- `prepare-tgos` 讀基線 offline-state 只核對內部一致與紀元，不核對程式指紋；這是允許的，因為帳本之後會以新程式重新匯入。
- `data/tgos/` 的送查檔與回傳是擁有者交換證據，不覆寫、不刪除。
- 不得刪除 tgos-state 的中間快照，除非已證明最新快照的 `expected_parent` 鏈不需要它們。
- 任何對 `lvr_pipeline/` 的修改都使之前 run 的程式指紋失效。這是設計，不是錯誤：下一次要發布的輸出前，再全量重跑並重做 V-10b 與 R08-2a 即可。
- 合成測試若因新檢查失敗，改用正確夾具，不加繞過旗標。
- 其他 session 可能同時在 D 段改 `lvr_pipeline/results/`。提交只用 `git add <明確路徑>`，改 00 或總表前先 `git diff` 確認乾淨。
