"""Real LOGIN-role readiness must require every effective business-table grant."""
import os

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from platform_backend_core import Database, MigrationRunner, TokenVerifier
from platform_backend_core.testing import temporary_database
from psycopg import sql
from psycopg.conninfo import make_conninfo

from platform_payment import MIGRATIONS, create_app

REQUIRED={'payment_intents': ('SELECT', 'INSERT', 'UPDATE'), 'payment_bindings': ('SELECT', 'INSERT'), 'payment_webhook_inbox': ('SELECT', 'INSERT'), 'payment_effects': ('SELECT', 'INSERT'), 'billing_checkouts': ('SELECT', 'INSERT', 'UPDATE'), 'billing_subscriptions': ('SELECT', 'INSERT', 'UPDATE'), 'billing_invoices': ('SELECT', 'INSERT', 'UPDATE'), 'payment_refunds': ('SELECT', 'INSERT', 'UPDATE'), 'payment_refund_effects': ('INSERT',), 'payment_pending_webhooks': ('SELECT', 'INSERT', 'UPDATE'), 'payment_ledger_entries': ('SELECT', 'INSERT')}
GRANTS=[('TABLE',table,privilege) for table,privileges in REQUIRED.items() for privilege in privileges]
GRANTS += [('SCHEMA','public','USAGE'),('TABLE','platform_schema_migrations','SELECT')]


class LocalAdapter:
    name='fixture'
    livemode=False
    def presign_put(self,*args):return {'url':'http://127.0.0.1/fixture','headers':{}}
    def head(self,key):return None
    def presign_get(self,*args):return 'http://127.0.0.1/fixture'
    def delete(self,key):pass
    def create(self,*,amount,currency,reference,idempotency_key,checkout):
        return {'id':'fixture_'+reference,'status':'pending','checkout_url':None}
    def verify_event(self,payload,signature):raise ValueError('fixture webhook disabled')


@pytest.fixture
def runtime():
    admin_dsn=make_conninfo(os.environ['PLATFORM_TEST_DSN'],passfile=os.devnull)
    with temporary_database(admin_dsn,prefix='ready_payment_') as temp:
        MigrationRunner(temp.admin_dsn,namespace='platform_payment').apply(MIGRATIONS)
        temp.grant_schema()
        with psycopg.connect(temp.admin_dsn) as conn:
            conn.execute('REVOKE USAGE ON SCHEMA public FROM PUBLIC')
            conn.execute(sql.SQL('ALTER ROLE {} LOGIN CONNECTION LIMIT 2').format(sql.Identifier(temp.runtime_role)))
            for table,privileges in REQUIRED.items():
                conn.execute(sql.SQL('REVOKE ALL ON {} FROM {}').format(sql.Identifier(table),sql.Identifier(temp.runtime_role)))
                conn.execute(sql.SQL('GRANT {} ON {} TO {}').format(sql.SQL(','.join(privileges)),sql.Identifier(table),sql.Identifier(temp.runtime_role)))
        login_dsn=make_conninfo(temp.admin_dsn,user=temp.runtime_role,options='')
        database=Database(login_dsn,min_size=1,max_size=1)
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        public=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
        verifier=TokenVerifier(issuer='fixture',audience='fixture',public_key=public)
        app=create_app(database,verifier,provider=LocalAdapter())
        try:
            with TestClient(app) as client:
                yield client,database,temp
        finally:
            database.close()


@pytest.mark.parametrize('kind,target,privilege',GRANTS)
def test_readiness_rejects_each_missing_effective_grant(runtime,kind,target,privilege):
    client,database,temp=runtime
    assert client.get('/readyz').status_code==200
    assert database.ready() is True
    with psycopg.connect(temp.admin_dsn) as conn:
        conn.execute(sql.SQL('REVOKE {} ON {} {} FROM {}').format(sql.SQL(privilege),sql.SQL(kind),sql.Identifier(target),sql.Identifier(temp.runtime_role)))
    assert client.get('/readyz').status_code==503
    with psycopg.connect(temp.admin_dsn) as conn:
        conn.execute(sql.SQL('GRANT {} ON {} {} TO {}').format(sql.SQL(privilege),sql.SQL(kind),sql.Identifier(target),sql.Identifier(temp.runtime_role)))
    for _ in range(2):
        assert client.get('/readyz').status_code==200
        assert database.ready() is True


def test_least_required_grants_do_not_need_destructive_permissions(runtime):
    client,database,temp=runtime
    with psycopg.connect(temp.admin_dsn) as conn:
        for table,privileges in REQUIRED.items():
            for privilege in ('SELECT','INSERT','UPDATE','DELETE','TRUNCATE'):
                if privilege not in privileges:
                    assert conn.execute('SELECT has_table_privilege(%s,%s,%s)',(temp.runtime_role,table,privilege)).fetchone()==(False,)
    assert client.get('/readyz').status_code==200
    assert database.ready() is True


def test_least_privilege_role_can_record_a_payment_effect(runtime):
    # INSERT ... RETURNING needs SELECT on the returned column; readiness must demand it.
    from uuid import uuid4
    _,database,temp=runtime
    workspace,intent=uuid4(),uuid4()
    with psycopg.connect(temp.admin_dsn) as admin:
        admin.execute("INSERT INTO payment_intents(id,workspace_id,actor_id,operation,idempotency_key,fingerprint,amount,currency,provider,provider_key,status) "
                      "VALUES(%s,%s,%s,'intent','k','f',100,'thb','stripe',%s,'pending')",(intent,workspace,uuid4(),'pk-'+str(intent)))
    with database.transaction(workspace) as conn:
        row=conn.execute("INSERT INTO payment_effects(workspace_id,intent_id,kind,amount,currency) VALUES(%s,%s,'payment_succeeded',100,'thb') ON CONFLICT DO NOTHING RETURNING intent_id",(workspace,intent)).fetchone()
    assert row[0]==intent
