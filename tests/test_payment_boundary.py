"""Payment-specific promises of contract.json that a later edit must not drop."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "contract.json").read_text(encoding="utf-8"))
INTERFACES = {item["id"]: item for item in CONTRACT["interfaces"]}


class PaymentBoundaryTests(unittest.TestCase):
    def test_cutover_needs_parity_idempotency_and_rollback_gates(self) -> None:
        gates = set(CONTRACT["migration_gates"])
        self.assertTrue(
            {"sandbox provider parity", "webhook idempotency", "ledger rollback"} <= gates
        )
        self.assertEqual(CONTRACT["migration"]["rollback"], "required")
        self.assertEqual(CONTRACT["migration"]["compatibility_adapter"], "required")

    def test_stripe_webhook_is_signature_verified_with_a_tolerance(self) -> None:
        auth = INTERFACES["payment.http.stripe-webhook"]["auth"]
        self.assertIn("Stripe-Signature", auth)
        self.assertIn("HMAC-SHA256", auth)
        self.assertRegex(auth, r"\b\d+ s tolerance\b")

    def test_every_provided_http_interface_declares_auth(self) -> None:
        for item in CONTRACT["interfaces"]:
            if item["kind"] == "http" and item["direction"] == "provides":
                with self.subTest(interface=item["id"]):
                    self.assertTrue(item.get("auth", "").strip())

    def test_ledger_writes_are_idempotent(self) -> None:
        auth = INTERFACES["payment.http.revenue-webhook"]["auth"]
        self.assertIn("Idempotency-Key", auth)

    def test_payment_depends_on_identity_security_and_observability(self) -> None:
        self.assertTrue({"identity", "security", "observability"} <= set(CONTRACT["depends_on"]))

    def test_contract_holds_no_secret_values(self) -> None:
        text = (ROOT / "contract.json").read_text(encoding="utf-8")
        for pattern in (r"sk_(live|test)_\w+", r"whsec_\w+", r"rk_(live|test)_\w+"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, text))


if __name__ == "__main__":
    unittest.main()
