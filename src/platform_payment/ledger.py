"""Double-entry settlement ledger in integer minor units.

Each verified settlement fact posts exactly one journal, identified by
(source_kind, source_id), inside the same transaction as the webhook inbox,
state transition and effect row. All legs go in one INSERT statement because the
database checks balance per statement (see migration 003); a failed posting
therefore rolls back the whole settlement and a redelivered event applies once.

Accounts:
- ``provider_clearing``: money held by the payment provider for us (asset).
- ``payment_revenue``: captured customer payments.
- ``payment_refunds``: refunds returned to customers (contra revenue).

Subscription invoices are provider metadata and are not posted here.
"""

from psycopg import sql

JOURNALS = {
    "payment_succeeded": (
        ("provider_clearing", "debit"),
        ("payment_revenue", "credit"),
    ),
    "refund_succeeded": (
        ("payment_refunds", "debit"),
        ("provider_clearing", "credit"),
    ),
}


def post(conn, *, workspace, source_kind, source_id, intent_id, amount, currency):
    if type(amount) is not int or amount < 1:
        raise ValueError("ledger amounts are positive integer minor units")
    legs = JOURNALS[source_kind]
    values = sql.SQL(",").join(
        sql.SQL("(%s,%s,%s,%s,%s,%s,%s,%s)") for _ in legs
    )
    params = []
    for account, direction in legs:
        params += [workspace, source_kind, source_id, intent_id, account, direction, amount, currency]
    conn.execute(
        sql.SQL(
            "INSERT INTO payment_ledger_entries"
            "(workspace_id,source_kind,source_id,intent_id,account,direction,amount,currency)"
            " VALUES {}"
        ).format(values),
        params,
    )
