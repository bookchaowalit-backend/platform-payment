import argparse
import os

from platform_backend_core import Database, MigrationRunner, TokenVerifier

from . import MIGRATIONS, create_app
from .live_mode import LiveModeRefused, refuse_live_mode


def main():
    parser = argparse.ArgumentParser(
        description="Staged platform_payment runtime; no automatic migrations"
    )
    parser.add_argument("command", choices=["serve", "migrate", "replay-webhooks", "relay-outbox"])
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    try:
        refuse_live_mode()
    except LiveModeRefused as refused:
        parser.exit(2, str(refused) + "\n")
    if args.command == "migrate":
        dsn = os.environ.get("DATABASE_MIGRATION_DSN")
        if not dsn:
            parser.error("DATABASE_MIGRATION_DSN required")
        try:
            applied = MigrationRunner(dsn, namespace="platform_payment").apply(
                MIGRATIONS
            )
        except Exception:  # noqa: BLE001 - migration errors are reported without the DSN
            parser.exit(
                1, "migration failed; inspect database access and migration history\n"
            )
        print(f"applied {len(applied)} migrations")
        return
    if args.command == "replay-webhooks":
        import json

        from .config import configured_provider
        from .pending_webhooks import replay

        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            parser.error("DATABASE_URL required")
        database = None
        try:
            provider = configured_provider()
            if provider is None:
                raise ValueError("provider required")
            database = Database(dsn, max_size=2)
            result = replay(database, provider, limit=args.limit)
        except Exception:  # noqa: BLE001 - replay errors are reported without configuration or event values
            parser.exit(
                1,
                "webhook replay failed; check staged configuration and pending records\n",
            )
        finally:
            if database is not None:
                database.close()
        print(json.dumps(result))
        return
    if args.command == "relay-outbox":
        import json

        from .outbox import KafkaPublisher, kafka_config_from_env, relay_once

        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            parser.error("DATABASE_URL required")
        database = None
        try:
            publisher = KafkaPublisher(kafka_config_from_env(os.environ))
            database = Database(dsn, max_size=2)
            result = relay_once(database, publisher, batch_size=args.limit)
        except Exception:  # noqa: BLE001 - relay errors are reported without configuration or event values
            parser.exit(1, "outbox relay failed; check PAYMENT_KAFKA_* and database access\n")
        finally:
            if database is not None:
                database.close()
        print(json.dumps({"sent": result.sent, "failed": result.failed, "skipped": result.skipped}))
        return
    dsn = os.environ.get("DATABASE_URL")
    issuer = os.environ.get("JWT_ISSUER")
    audience = os.environ.get("JWT_AUDIENCE")
    pem = os.environ.get("JWT_PUBLIC_KEY")
    jwks = os.environ.get("JWT_JWKS_URL")
    if not dsn or not issuer or not audience or not (pem or jwks):
        parser.error(
            "DATABASE_URL, JWT_ISSUER, JWT_AUDIENCE and JWT_PUBLIC_KEY or JWT_JWKS_URL required"
        )
    try:
        verifier = TokenVerifier(
            issuer=issuer, audience=audience, public_key=pem, jwks_url=jwks
        )
        database = Database(dsn, max_size=int(os.environ.get("DATABASE_POOL_MAX", "8")))
        app = create_app(database, verifier)
        from . import gateway

        # SPEC-002 (platform-api-gateway): only calls that came through the edge gateway.
        gateway.install(app, gateway.GatewayCheck.from_env('platform-payment'))
    except Exception:  # noqa: BLE001 - startup errors are reported without configuration values
        parser.exit(1, "invalid service configuration\n")
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        access_log=False,
        limit_concurrency=64,
        timeout_keep_alive=5,
    )


if __name__ == "__main__":
    main()
