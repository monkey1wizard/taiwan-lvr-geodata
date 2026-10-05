# 公開快照發布與指標復原

P2 使用 GitHub 公開 Release 附件。程式、來源描述及小型版本指標進 Git，raw、官方地址 CSV、工作索引與生成 output 留在 Git 外。GitHub 的 Release 附件上限為每檔小於 2 GiB、每個 Release 最多 1,000 個附件，查核日期為 2026-10-05。見 [About releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。

發布程式要求 repo 已啟用 [Release immutability](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)。資料使用唯一版本標籤，先建立 draft、上傳完整附件，再從 GitHub 下載核對全部附件，最後公開成不可變 Release。缺憑證、傳送失敗或 hash 不一致時不更新 Git 指標。

先在正式 checkout 的 main 提交及推送 producer 程式，再用該 commit 建置真實資料。若 producer 有未提交原始程式碼，程式拒絕發布。以目前遠端 main commit 設定預期 parent。

```bash
uv run --locked --python 3.13.16 python -m lvr_pipeline publish-output --input data/output/SNAPSHOT_ID --expected-parent PRODUCER_COMMIT --checkout . --receipt data/work/release-receipt.json
uv run --locked --python 3.13.16 python -m lvr_pipeline commit-release-pointer --receipt data/work/release-receipt.json --checkout .
```

第一個命令只在全部附件核對且公開成功後產生 receipt。第二個命令要求本機 main 乾淨且仍符合 expected parent，提交 `data/releases/latest.json` 與版本指標，再直接非強制 push main。遠端已前進時，Git 拒絕更新，不能 force push。舊指標仍存在於 Git 歷史，舊版本指標及不可變 Release 繼續可取得。

若上傳中斷，以同一個未修改候選與相同 parent 重試。程式只接續相同 producer 的 draft，逐項核對已存在附件再補傳缺件，不替換不同雜湊的附件。若候選內容已修改，使用新 snapshot ID。若 Release 已公開但 main 同時前進，保留舊指標並重新核對最新 main，另提交完整新版本指標。不要重用已公開標籤或替換附件。

一般檔案讀取使用者不需要發布憑證。`gh` 使用本機登入，環境憑證不得寫入 NOTICE、manifest、receipt 或日誌。無發布權限的工作環境只能產生候選及驗證結果，不能宣稱公開交付完成。
