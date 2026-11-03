# ADR-0001 — FastAPI service บน platform-backend-core ที่ pin เป็น commit

- **Status:** accepted (retro-documented)
- **วันที่:** runtime เข้ามากับ PR #1 (merge `bfd4f7d`, 2026-10-29); repin เป็น `21d5f44` ใน `b9e9be1`

## Context

platform services ทุกตัวใช้ runtime กลางเดียวกัน (FastAPI lifecycle, auth, DB pool) แต่ต้อง build ซ้ำได้และไม่เสี่ยง dependency confusion เพราะ `platform-backend-core` ไม่อยู่บน PyPI

## Decision

- `create_app` ของ service ห่อ `platform_backend_core.create_app("platform-payment", database, verifier)` และแทน `/readyz` ด้วย readiness ของตัวเอง
- core ถูก pin เป็น commit `21d5f44d26220080cbed43b9807979dc84a8533a` ในสามที่: `[tool.uv.sources]`, `Dockerfile` (`pip wheel 'platform-backend-core @ git+...@21d5f44...'`), และ `uv.lock`; `tests/test_build_hygiene.py` fail ถ้าไม่ตรงกัน
- service เป็นเจ้าของโค้ดตัวเอง ไม่ import module ของ platform อื่นหรือ hub (`README.md` §Boundary)

## Consequences

- อัปเกรด core = เปลี่ยน commit พร้อมกันทุกที่ (มี test กัน)
- พฤติกรรมสำคัญมาจาก core: body 1 MiB, timeout 30 s, `/metrics` ไม่มี auth, `_Boundary` กลืน exception เป็น 500 (จึงต้องมี `CaptureErrors`)

## Evidence

`src/platform_payment/app.py`, `pyproject.toml`, `Dockerfile`, `uv.lock`, `tests/test_build_hygiene.py`, `docs/runtime.md` §Container image, platform-backend-core `21d5f44` `src/platform_backend_core/http.py`, `git log` (`79635ea`, `b9e9be1`)
