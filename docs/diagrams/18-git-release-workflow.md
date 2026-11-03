# 18 — Git and release workflow

ข้อเท็จจริงจาก `git log origin/main` และ GitHub API (2026-11-03):

- `main` ไม่มี branch protection (`GET .../branches/main/protection` → 404 "Branch not protected") และไม่มี ruleset (0)
- PR ทั้งหมด 2 ตัว: #1 (`codex/shared-infrastructure-20261015`) **closed ไม่ merge** ภายใต้ D8 แล้วเนื้อหาถูก rebase เป็น branch `payment-runtime` และ merge แบบ local (`bfd4f7d` "Merge test-mode payment runtime with balanced ledger (PR #1)"); #2 (`ci/uv-lock-mypy-sentry`) merged 2026-11-03 04:45 UTC เป็น commit เดียว `b0dfd8d` บน `324b1f8` (ไม่มี merge commit)
- branch อื่นที่ merge แบบ local: `build-hygiene` (`8e90fe5`), `repin-core` (`80fc404`), `repin-core-2` (`324b1f8`), `claude/untitled-session-bhlj06` (`3ba3c8d`)
- commit ตรงบน main ก็มี (เช่น `065db9c` docs API.md)
- ไม่มี tag หรือ GitHub release; version ใน `pyproject.toml` คงที่ `0.1.0`
- deploy ไม่ผูกกับ git: `railway up` จาก `git archive` (ops record deploy recipe), deployment meta ไม่มี commit hash

```mermaid
gitGraph
  commit id: "604a359 scaffold"
  commit id: "c0318a0 ci check"
  branch claude-session
  commit id: "contract work"
  checkout main
  merge claude-session id: "3ba3c8d"
  commit id: "1704cdd shared CI v1.1.1"
  commit id: "99c17c4 Ruff EXE001"
  branch payment-runtime
  commit id: "d1825c2 to 0dc1385 PR1 work"
  commit id: "b22ef2a live-mode refusal"
  commit id: "f9a3796 ledger"
  checkout main
  merge payment-runtime id: "bfd4f7d"
  branch build-hygiene
  commit id: "a9de897 image"
  checkout main
  merge build-hygiene id: "8e90fe5"
  branch repin-core
  commit id: "79635ea core 88306b2"
  checkout main
  merge repin-core id: "80fc404"
  branch repin-core-2
  commit id: "b9e9be1 core 21d5f44"
  checkout main
  merge repin-core-2 id: "324b1f8"
  commit id: "b0dfd8d PR2, no merge commit"
  commit id: "065db9c API.md"
```

```mermaid
flowchart LR
  A["Work branch or direct commit"] --> B["push: check.yml and shared-runtime.yml run"]
  B --> C["Local merge into main, or PR merge"]
  C --> D["push main: CI runs again, not a required gate"]
  D --> E["Manual: git archive main, railway up --ci"]
  E --> F["Railway build from Dockerfile, healthcheck /healthz"]
  F --> G["Manual check: /healthz and /readyz"]
```

Sources: `git log --graph origin/main`, `gh pr list -R bookchaowalit-backend/platform-payment --state all`, `gh api repos/bookchaowalit-backend/platform-payment/branches/main/protection`, `gh api .../rulesets`, `.github/workflows/*.yml`, `docs/runtime.md` §Verification 2026-10-29 (rebase of PR #1), Railway API read 2026-11-03, ops record: book-platforms-status (2026-11-03)

หมายเหตุ: gitGraph ย่อ commit ภายใน branch; ลำดับ merge ตรงกับ `git log --merges`

## Open questions

- ควรเปิด branch protection / required checks บน `main` หรือไม่ (repo public, CI บน GitHub-hosted)
- ควร tag release หรือส่ง `SENTRY_RELEASE` ตอน `railway up` เพื่อผูก deploy กับ commit หรือไม่ (`RAILWAY_GIT_COMMIT_SHA` ไม่มีเมื่อ deploy ด้วย CLI)
