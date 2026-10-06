# Agent execution contract

Use docs/plans/重建企劃.md for task dependencies and acceptance points. GAL and local .dev files are optional. Copy docs/task-result-template.md into docs/records/ and record commands and pass/fail/not-run there. Keep the template blank. Use R work packages and V acceptance points; historical T-task completion is not rebuild acceptance. Do not mark a task accepted from implementation alone.

Run bash scripts/setup.sh in Linux with uv installed. Tests use synthetic inputs and need no credentials or production data. Never download real data during installation or tests. Keep the retained legacy entry points compatible. Use building_key_v2 and validated_roc_to_tx_yyyymm for the new contracts.

Do not infer transaction identity, revision order, sub-door equivalence, coordinate axes or source permissions. Preserve source observations when identity is unproven. Keep secrets, raw ZIPs, generated snapshots and third-party address rows out of Git. Do not publish or update the related address repo as part of P0.

For local development, use C:/Code/taiwan-lvr-geodata as the authoritative checkout. Work on main, test and commit locally, then push directly to origin/main. Do not create scratch development copies, phase branches or pull requests unless the owner explicitly asks. GitHub Actions provides Linux verification.
