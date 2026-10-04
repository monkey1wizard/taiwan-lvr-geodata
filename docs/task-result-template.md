# Cloud task 執行結果範本

依完整企劃的 task 前置條件與 test points 填寫。執行環境不需要 GAL 或本機 .dev。這份範本沒有宣稱任何 task 已完成。

## Task 與前置條件

| 項目 | 填寫內容 |
| --- | --- |
| Task ID／phase | T-NN／PN |
| 狀態 | Planned／Running／Accepted／Failed／Waiting |
| 程式 repo／commit／PR | 實際版本與可審查變更 |
| 前置 tasks | 每項已驗收交付成果及版本 |
| 執行範圍 | 樣本／指定縣市類別／單季／全清單 |
| 環境 | OS、Python／套件版本、CPU、記憶體、可用磁碟 |
| 相關決策 | 沿用 GitHub 公開與舊 TGOS 規則，記錄此 task 實測資源及必要憑證存取方式，不寫憑證值 |

## 輸入與命令

記錄輸入 manifest URI／SHA-256、檔案雜湊、結構／地址鍵版本、來源 commit、parent snapshot 與設定。只填可取得且已驗證的位置。憑證內容不寫入報告。

逐項填寫已實作的命令、工作目錄、參數及結束代碼。不能把企劃中的預計命令當成現有命令。

## Test points

| Test point | 實際命令或人工檢查 | 結果 | 證據及限制 |
| --- | --- | --- | --- |
| TP-NN | 完整命令／輸入 | pass／fail／not-run | 結果檔案、預期／實際差異、未測範圍 |

若相關 test point 失敗或尚未執行，該 task 尚未驗收。實作程式、樣本測試及真實部署分別記錄，不混稱全部通過。

## Output data 與資源

| 項目 | 填寫內容 |
| --- | --- |
| Snapshot／manifest | ID、parent、SHA-256、schema／producer 版本 |
| 資料粒度 | source_observation 或已證明的交易識別，說明空 transaction_key |
| 範圍與完整性 | 選定／缺失來源、交易月 tx_yyyymm、各類別筆數、已定位／未定位／衝突 |
| 月下載 | YYYYMM_category 檔名、格式、公開 URL、雜湊及 month_coverage_status |
| 年度包 | 年份／格式、包含／缺失／空月份、曆年完整性、原月檔雜湊與分片索引 |
| 更新對應 | 回補影響月份及重建的年度包，未變檔案雜湊與前版 |
| TGOS 狀態 | 初始 tgos_started=false，開始後須有保留／查詢帳本及雜湊 |
| 來源核對 | 原始列的保留／排除／失敗去向，以及成員外部索引鍵 |
| 檔案 | 相對路徑、位元組數、SHA-256、讀回結果 |
| 資源 | 執行時間、RSS 峰值、下載量、暫存／磁碟峰值、剩餘空間 |
| 小型輸出評估 | 月檔／年包／維護狀態大小、暫存峰值、Git 歷史成長及 Git／Release 配置 |
| 交接位置 | 可取得的 GitHub 公開 URL、版本／雜湊及保留紀錄；artifact 記錄到期條件 |

若輸出只存在會消失的工作目錄，就標示「尚未持久交接」。GitHub 公開方向已確定，發布前核對月／年與維護附件完整性，再更新版本索引；上傳失敗保留前版。一般使用者只下載月檔或年包，不要求下載整份維護狀態。憑證不進公開資料。

## 結論與下一個 task

說明已完成的範圍、尚未驗收的 test points、實際阻擋及可接續的 task。指明下一位 agent 如何取得相同輸入及輸出。採用已驗證離線快照時，說明是否需要 raw ZIP 重建，不能省略不相容檢查。
