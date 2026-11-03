# 11 — State machines

## payment_intents.status

```mermaid
stateDiagram-v2
  [*] --> creating: POST intent or checkout
  creating --> pending: provider ok, binding committed
  creating --> unknown: provider timeout or error
  unknown --> pending: reconcile claims new fence, provider ok
  unknown --> unknown: reconcile, provider error again
  pending --> succeeded: verified webhook succeeded
  pending --> failed: verified webhook failed or canceled
  pending --> pending: processing event
  failed --> succeeded: later verified success supersedes failure
  succeeded --> [*]
```

กฎใน `payment_events.handle`: ไม่เปลี่ยนเมื่อ `succeeded` แล้ว; จาก `failed` ไปได้แค่ `succeeded`; reconcile ถูกปฏิเสธเมื่อ `created_at` เกิน 20 ชั่วโมง

Sources: `src/platform_payment/app.py`, `payment_events.py`, `migrations.py` (CHECK)

## payment_refunds.status

```mermaid
stateDiagram-v2
  [*] --> creating: amount reserved
  creating --> pending: provider ok
  creating --> unknown: provider error, reservation kept
  unknown --> pending: reconcile ok
  pending --> succeeded: signed refund succeeded, reserved to refunded
  pending --> failed: signed refund failed or canceled, reservation released
  succeeded --> [*]
  failed --> [*]
```

Sources: `src/platform_payment/refunds.py`, `provider_operations.py`, `stripe_events.py`, `billing_migrations.py`

## billing_checkouts.status

```mermaid
stateDiagram-v2
  [*] --> creating
  creating --> pending: session created
  creating --> unknown: provider error
  unknown --> pending: reconcile ok
  pending --> complete: checkout.session.completed or async_payment_succeeded
  pending --> expired: checkout.session.expired or async_payment_failed
  complete --> [*]
  expired --> [*]
```

Sources: `src/platform_payment/billing_checkouts.py`, `provider_operations.py`, `billing_events.py` (`checkout`), `stripe_events.py`

## billing_subscriptions.status และ cancel_status

```mermaid
stateDiagram-v2
  [*] --> incomplete: checkout complete
  incomplete --> active: newer signed snapshot
  incomplete --> trialing
  incomplete --> incomplete_expired
  trialing --> active
  active --> past_due
  past_due --> active
  past_due --> unpaid
  active --> paused
  paused --> active
  active --> canceled
  past_due --> canceled
  unpaid --> canceled
  canceled --> [*]
  incomplete_expired --> [*]
```

status ใด ๆ ใน CHECK list เปลี่ยนไปเป็นค่าอื่นได้ตาม snapshot ที่ `created` ใหม่กว่า (ภาพแสดงเส้นทางปกติ ไม่ใช่ข้อจำกัดของโค้ด); `canceled` และ `incomplete_expired` เป็น terminal; snapshot เวลาเดียวกันที่ขัดกัน → 409

```mermaid
stateDiagram-v2
  [*] --> none
  none --> creating: POST cancel with Idempotency-Key
  creating --> pending: provider ack
  creating --> unknown: provider error
  unknown --> pending: cancel reconcile
  pending --> confirmed: snapshot cancel_at_period_end true or canceled
  none --> confirmed: snapshot shows cancel without our request
  confirmed --> [*]
```

Sources: `src/platform_payment/subscriptions.py`, `billing_events.py` (`subscription`), `billing_migrations.py`

## billing_invoices.status

```mermaid
stateDiagram-v2
  [*] --> draft
  [*] --> open
  draft --> open
  open --> paid
  open --> void
  open --> uncollectible
  paid --> [*]
  void --> [*]
  uncollectible --> [*]
```

paid/void/uncollectible เป็น terminal; late `draft` ไม่แทน `open` ได้; `paid` ต้อง amount_paid = amount_due

Sources: `src/platform_payment/billing_events.py` (`invoice`), `billing_migrations.py`

## payment_pending_webhooks.state

```mermaid
stateDiagram-v2
  [*] --> pending: verified event staged
  pending --> pending: redelivery or replay, attempts plus 1 up to 100
  pending --> applied: settlement committed or unbound event ignored
  applied --> [*]
```

replay อัตโนมัติหยุดเมื่อ attempts ≥ 20

Sources: `src/platform_payment/pending_webhooks.py`, `payment_events.py` (`ignore_unbound`), `billing_migrations.py`
