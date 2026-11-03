# ADR-0008 — Sentry: errors only, ไม่ส่ง free text, ไม่มี tracing

- **Status:** accepted (retro-documented)
- **วันที่:** `b0dfd8d` (PR #2, 2026-11-03); pattern เดียวกับ platform-security `0187719`

## Context

service นี้ถือ provider credential และ JWT; regex scrubbing ใน platform-security ถูก bypass ซ้ำหลายรอบ (PR #4–#13) จึงเลือก fail closed

## Decision

- `init_sentry` ทำงานเมื่อมี `SENTRY_DSN` เท่านั้น; `traces_sample_rate=None`, `trace_propagation_targets=[]`, `send_default_pii=False`, `include_local_variables=False`, `max_request_body_size="never"`, logging integration ปิด
- `_scrub_event` ตัด exception value, message, logentry, breadcrumbs, extra, user, request (เหลือแค่ method), dynamic sampling context
- `CaptureErrors` เป็น innermost middleware เพราะ core `_Boundary` แปลง exception เป็น 500 ก่อนถึง integration ของ Sentry

## Consequences

- event มีแค่ type, stack (file/function/line), route template, tags (`service`), release (`RAILWAY_GIT_COMMIT_SHA` หรือ `SENTRY_RELEASE`)
- การ debug ต้องอาศัย stack ไม่มีข้อความ error

## Evidence

`src/platform_payment/_sentry.py`, `src/platform_payment/__init__.py`, `app.py` (`CaptureErrors`), `runtime_tests/test_sentry.py`, ops record: book-platforms-status (2026-11-03)
