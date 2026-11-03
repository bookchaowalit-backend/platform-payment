# 15 — CI/CD pipeline

workflows ทั้งสองรันบน `push` (ทุก branch), `pull_request` และ `workflow_dispatch`; `permissions: contents: read`; concurrency ต่อ ref แบบ cancel-in-progress
ไม่มี job ใด deploy: deploy เป็นขั้น manual `railway up` (ดู 16, 18)

```mermaid
flowchart LR
  T["push, pull_request, workflow_dispatch"] --> CK["check.yml"]
  T --> SR["shared-runtime.yml"]
  subgraph CK1["check.yml"]
    C1["contract: ci-python v1.1.1, Python 3.11 and 3.12, Ruff, bash scripts/check.sh"]
    C2["python: ci-python v1.1.1, Python 3.12, uv sync frozen, Ruff, mypy, PostgreSQL service, pytest with coverage gate 87, uv build wheel"]
    C3["security: security.yml v1.1.1"]
  end
  subgraph SR1["shared-runtime.yml"]
    I1["image: docker build --pull, no push"]
    I2["import check and non-root uid"]
    I3["CLI help lists serve, migrate, replay-webhooks"]
    I4["live-key probe exits 2 without echoing value"]
    I1 --> I2 --> I3 --> I4
  end
  CK --> C1
  CK --> C2
  CK --> C3
  SR --> I1
  C1 -.-> M["Manual: railway up --ci from git archive"]
  C2 -.-> M
  I4 -.-> M
  M --> RW["Railway payment-api"]
```

| Job | Uses | Notes |
|---|---|---|
| `contract` (matrix 3.11, 3.12) | `bookchaowalit/platform-ci-workflows/.github/workflows/ci-python.yml@87dc5e1c` (v1.1.1) | `install-command: "true"`, `test-command: bash scripts/check.sh` (schema, pin, unittest `tests/`) |
| `python` | same reusable workflow | `postgres: true`, `coverage-threshold: "87"` (measured 87.06% 2026-11-03), `PLATFORM_TEST_DSN="$DATABASE_URL" python -m pytest -q --cov`, `uv build --wheel` |
| `security` | `security.yml@87dc5e1c` (v1.1.1) | shared scan (gitleaks ตาม `docs/runtime.md`) |
| `image` | `actions/checkout@11d5960a` (v4) | build-only, 15 min timeout |

Sources: `.github/workflows/check.yml`, `.github/workflows/shared-runtime.yml`, `scripts/check.sh`, `pyproject.toml`, `tests/test_build_hygiene.py`, `docs/runtime.md` §Container image + §Verification 2026-10-29, ops record: book-platforms-status (2026-11-03) (deploy recipe)

## Open questions

- รายละเอียด step ภายใน `ci-python.yml` / `security.yml` v1.1.1 อยู่ใน repo `platform-ci-workflows` (ไม่ได้อ่านในรอบนี้)
- ไม่มี CD; ควรมี deploy job หรือ deploy-smoke หลัง `railway up` หรือไม่
