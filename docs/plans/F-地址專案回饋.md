# F 地址專案回饋

類別 iii：不論 TGOS 情況都能持續做。修訂日期：2026-10-20。現況與擁有者決定以 [00 主企劃](00-主企劃.md)與[擁有者決定與驗收總表](擁有者決定與驗收總表.md)為準。本文件取代原「地址專案匯入企劃」，R11-1 已完成，R11-2、R11-3、V-14 對應 f02 到 f04。

## 目的

把本專案經新規則核驗的 TGOS 與人工查核座標，整理成地址資料庫專案 `taiwan-address-data` 的補充候選，交給擁有者決定是否匯入。本專案不修改地址專案、不切它的 HEAD，不在 P0 範圍發布或更新地址專案。候選產生不等於已匯入；真實匯入前要完成全基底重複稽核，跨專案結果獨立記錄。

## 前置

- D 段完成，已於 10-18 達成。
- 候選來源是 e02 之後的新基線 run 的 `final-state`：TGOS 採用的門牌與擁有者 `adopted` 判定的門牌。過渡基線的結果不作來源。

## 交接格式

14 欄 CSV，欄名與順序固定，見[地址規則](../地址規則.md)「地址補充交接」：`FULL_ADDR,COUNTY,TOWN,VILLAGE,NEIGHBORHOOD,ROAD,SECTION,LANE,ALLEY,SUB_ALLEY,TONG,NUMBER,X,Y`。`X` 為經度，`Y` 為緯度。缺少行政代碼時不得編造。

| 候選與地址基底的關係 | 分類與後續 |
| --- | --- |
| 基底中不存在且證據有效 | 新增候選 |
| 基底已存在且座標相同 | 依本次新證據建立補充候選，不重複新增 |
| 基底已存在但有效座標需更新 | 更新候選，保留基底值、候選值與依據 |
| 地址不符、代碼缺失或多解、座標無效或衝突 | 隔離，保留原因 |

## 工作

- [x] f01 10-12 `address_patch.py` 純搬移至 `lvr_pipeline/results/address_patch.py` 並更新引用，見[紀錄](../records/程式目錄整理.md)
- [ ] f02 冪等與隔離：同一份 `final-state` 重複執行 `export_address_patch` 不重複新增；行政代碼缺失或多解時隔離並保留原因；TGOS 採用列帶 `response_building_key` 與回傳座標，人工判定列帶判定檔雜湊。合成案例：新增、補充不重複、更新、四種隔離、重複執行
- [ ] f05 候選來源改讀新基線 `final-state` 的 `coordinate_sources.parquet`，不讀 tgos-state 或 review 的中間結果。`verify_address_patch` 核對每列可追溯到 `transaction_id` 加 `door_index`
- [ ] f06 以真實資料從新基線產生候選，統計四類筆數、各縣市分布，抽樣 20 筆人工看，寫紀錄
- [ ] f03 全地址基底重複稽核：候選對地址專案釘選版本的全部路名 CSV 逐筆比對，確認「新增」沒有重複、「更新」的基底值等於當前值。結果獨立記錄。真實匯入由擁有者操作或明確同意，另記跨專案匯入版本、commit 與結果
- [ ] f04 V-14：以本次新重建產物進行地址專案候選與跨專案匯入驗收，候選可追溯、分類計數相符、匯入版本有紀錄

## 擁有者檢查點

- f03 匯入前同意。
- 匯入後地址專案會有新 commit，本專案是否重新釘選並重跑，依總表 g04 的決定。

## 程式事實與行號

以 HEAD `e94e1c3` 查定，施做前先 `grep` 確認。

| 事實 | 位置 |
| --- | --- |
| `export_address_patch(state, area_files, work_dir, run_id)`、`verify_address_patch(path)` | `lvr_pipeline/results/address_patch.py:125`、`:208` |
| 舊版回傳地址拆解 `_split_legacy`，依行政區表判定 | `address_patch.py:51` |
| 14 欄與映射的權威文字 | `docs/地址規則.md`「地址補充交接」 |
| `final-state` 的 `coordinate_sources.parquet` 與 `affected_months.json` | D 段 d08 到 d11，`lvr_pipeline/results/final_state.py` |

## 陷阱

- 不讀過渡基線或 tgos-state 的中間結果當候選，只讀新基線 `final-state`。
- 不修改 `C:/Code/taiwan-address-data`，不切它的 HEAD。候選 CSV 放 `data/tmp/f/`，不進 Git，因為含第三方地址來源列。
- 匯入後地址專案的資料檔雜湊會變，本專案的釘選檢查會拒絕執行，直到重新釘選。
