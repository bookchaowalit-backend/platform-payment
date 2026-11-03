"""Sentry is off without SENTRY_DSN; with it: errors only, no PII, locals, bodies or secrets."""
import sys
from typing import Any

import pytest
import sentry_sdk

import platform_payment
from platform_payment import _sentry


def _clear_env(monkeypatch):
    for name in ('SENTRY_DSN', 'SENTRY_ENVIRONMENT', 'SENTRY_RELEASE', 'RAILWAY_GIT_COMMIT_SHA', 'RAILWAY_SERVICE_NAME'):
        monkeypatch.delenv(name, raising=False)


def test_package_import_initialises_sentry():
    # The package __init__ wires init_sentry, so `python -m platform_payment serve` reports errors.
    assert platform_payment._init_sentry is _sentry.init_sentry


def test_no_dsn_is_a_noop(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv('SENTRY_DSN', '   ')
    calls = []
    fake = type('Fake', (), {'init': staticmethod(lambda **kw: calls.append(kw))})
    monkeypatch.setitem(sys.modules, 'sentry_sdk', fake)
    _sentry.init_sentry()
    assert calls == []


def test_dsn_initialises_errors_only_without_pii(monkeypatch, capsys):
    _clear_env(monkeypatch)
    monkeypatch.setenv('SENTRY_DSN', 'https://public@o0.ingest.sentry.io/0')
    monkeypatch.setenv('RAILWAY_SERVICE_NAME', 'payment-api')
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_SHA', 'abc123')
    calls: list[dict[str, Any]] = []
    tags: dict[str, str] = {}
    monkeypatch.setattr(sentry_sdk, 'init', lambda **kw: calls.append(kw))
    monkeypatch.setattr(sentry_sdk, 'set_tag', lambda key, value: tags.__setitem__(key, value))
    _sentry.init_sentry()
    (kw,) = calls
    assert kw['environment'] == 'production'
    assert kw['release'] == 'abc123'
    assert kw['traces_sample_rate'] is None
    # No tracing also means no forwarding of incoming sentry-trace/baggage (user ids) on our own
    # outbound requests, such as the JWKS fetch.
    assert kw['trace_propagation_targets'] == []
    assert kw['send_default_pii'] is False
    assert kw['include_local_variables'] is False
    assert kw['max_request_body_size'] == 'never'
    assert kw['before_send'] is _sentry._scrub_event
    (logging_integration,) = kw['integrations']
    assert logging_integration._handler is None
    assert logging_integration._breadcrumb_handler is None
    assert logging_integration._sentry_logs_handler is None
    assert tags == {'service': 'payment-api'}
    captured = capsys.readouterr()
    assert 'sentry: enabled for payment-api' in captured.err
    assert captured.out == ''
    assert 'ingest.sentry.io' not in captured.err


def test_release_is_unset_without_a_git_sha(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv('SENTRY_DSN', 'https://public@o0.ingest.sentry.io/0')
    calls = []
    monkeypatch.setattr(sentry_sdk, 'init', lambda **kw: calls.append(kw))
    monkeypatch.setattr(sentry_sdk, 'set_tag', lambda key, value: None)
    _sentry.init_sentry()
    assert calls[0]['release'] is None


def test_scrub_event_drops_free_text_request_details_user_and_extra():
    event = {
        'exception': {'values': [{'value': 'bad url postgres://u:pw@h/db for a@b.example'}]},
        'message': 'contact a@b.example',
        'request': {
            'method': 'POST',
            'url': 'https://x/api?token=t',
            'data': {'address': '1 Main Rd'},
            'headers': {'authorization': 'Bearer t', 'x-forwarded-for': '198.51.100.1'},
            'cookies': {'session': 's'},
            'query_string': 'token=t',
            'env': {'REMOTE_ADDR': '198.51.100.1'},
        },
        'user': {'ip_address': '198.51.100.1', 'email': 'a@b.example'},
        'breadcrumbs': {'values': [{'message': 'GET with Bearer abc', 'data': {'url': 'https://x?token=t'}}]},
        'extra': {'sys.argv': ['--token', 't']},
    }
    out = _sentry._scrub_event(event, {})
    assert out['exception']['values'] == [{}]
    assert 'message' not in out
    assert out['request'] == {'method': 'POST'}
    assert 'user' not in out
    assert 'breadcrumbs' not in out
    assert 'extra' not in out
    for value in ('198.51.100.1', 'a@b.example', '1 Main Rd', 'token=t'):
        assert value not in str(out)


def test_scrub_event_drops_log_text():
    token = 'opaque' + '-token-value'
    event = {
        'logentry': {
            'message': 'Authorization: Bearer %s',
            'params': [token],
            'formatted': 'Authorization: Bearer ' + token,
        }
    }
    out = _sentry._scrub_event(event, {})
    assert 'logentry' not in out
    assert token not in str(out)


def test_scrub_event_drops_dynamic_sampling_context():
    event = {'contexts': {'trace': {'trace_id': 't', 'dynamic_sampling_context': {'user_id': 'alice'}}}}
    out = _sentry._scrub_event(event, {})
    assert out['contexts']['trace'] == {'trace_id': 't'}


@pytest.mark.parametrize('value', [None, 42])
def test_scrub_event_tolerates_missing_or_odd_fields(value):
    event = {'exception': {'values': [{'value': value}, 'odd']}, 'request': 'x', 'logentry': None}
    out = _sentry._scrub_event(event, {})
    assert out['exception']['values'] == [{}, 'odd']


def test_a_server_500_reaches_sentry_scrubbed(monkeypatch):
    # The shared core's boundary turns unexpected exceptions into a redacted 500 before an outer
    # ASGI layer sees them, so the app must capture them itself (inside that boundary).
    from uuid import uuid4

    from fastapi.testclient import TestClient
    from platform_backend_core import Principal

    import platform_payment.app as payment_app
    from platform_payment import create_app

    class _Verifier:
        def verify(self, authorization):
            return Principal(uuid4(), uuid4(), frozenset(['payments:read']))

    class _Database:
        def close(self):
            pass

        def ready(self):
            return True

    events: list[dict[str, Any]] = []

    class _Memory(sentry_sdk.transport.Transport):
        def capture_envelope(self, envelope):
            events.extend(item.payload.json for item in envelope.items if item.type == 'event')

    real_init = sentry_sdk.init
    # The production configuration, with only the transport swapped for memory.
    monkeypatch.setattr(sentry_sdk, 'init', lambda **kw: real_init(transport=_Memory, **kw))
    monkeypatch.setenv('SENTRY_DSN', 'https://k@o0.ingest.sentry.io/1')
    _sentry.init_sentry()
    # Built from parts so the source lines Sentry attaches do not contain the values either.
    message = 'db password=' + 'pw-' + 'SRV500' + ' card ' + '4242' + '4242' + '4242' + '4242'
    try:
        def broken(*args, **kwargs):
            raise RuntimeError(message)
        monkeypatch.setattr(payment_app, 'transaction', broken)
        with TestClient(create_app(_Database(), _Verifier())) as c:
            r = c.get('/v1/payment-intents', headers={'Authorization': 'Bearer fixture'})
        assert r.status_code == 500 and r.json() == {'detail': 'internal server error'}
        sentry_sdk.flush()
        assert len(events) == 1
        # Reported as unhandled, so unhandled-error filters and alerts still see server failures.
        assert events[0]['exception']['values'][-1]['mechanism']['handled'] is False
        text = str(events[0])
        assert 'RuntimeError' in text and 'pw-SRV500' not in text and '4242' '4242' not in text
    finally:
        # dsn=None would read SENTRY_DSN (still set here) and start a real, unscrubbed client.
        real_init(dsn='')
    assert sentry_sdk.get_client().transport is None  # nothing can leave the test process


# Every secret-bearing shape the review rounds found (PRs #4-#13 and after). Fragments are joined at
# runtime so no source line Sentry attaches holds a whole marker.
_SECRET_MESSAGES = [
    ('password=' + 'hunter2' + '-M01', 'hunter2-M01'),
    ("password='a\\' b " + 'SEC' + "RET-M02'", 'RET-M02'),
    ('Authorization: Bearer ' + 'tok' + '-M03', 'tok-M03'),
    ('{"url":"https://h/?x=1","password":"prefix ' + 'SEC' + 'RET-M04"}', 'RET-M04'),
    ('postgresql://u:pa ' + 'ss' + '-M05@h/db', 'ss-M05'),
    ('see https://h/?*** and https://u:***@h/x?token=' + 'tok' + '-M06', 'tok-M06'),
    ("cfg password=\\'unterminated " + 'SEC' + 'RET-M07', 'RET-M07'),
    ('connect host=db password=a\\\\ ' + 'SEC' + 'RET-M08', 'RET-M08'),
    ('from ' + 'buyer' + '-M09@example.com at 203.0.113.' + '77', 'buyer-M09'),
    # earlier rounds' scrubber regressions, now sent through the real client
    # a credential straddling the length cap must not survive as a partial token
    ('x ' * 4080 + 'postgresql://u:' + 'S' * 40 + '@h/db', 'SSSSSSSSSS'),
    # past the pattern bounds the whole URL token is redacted (fail closed)
    ('dial postgresql://' + 'u' * 300 + ':pw-HHH888@h/db', 'pw-HHH888'),
    ('GET https://h.example/' + 'p' * 3000 + '?code=c-III999 failed', 'c-III999'),
    ('dial postgresql://u:' + 'P' * 600 + '@h/db', 'PPPPPPPPPP'),
    ('from ' + 'L' * 100 + '@example.com', 'LLLLLLLLLL'),
    # one scrubbed URL must not vouch for an unscrubbed one in the same token
    ('a://x:pw-OK1@h/b://' + 'u' * 300 + ':pw-ZZZ000@h2', 'pw-ZZZ000'),
    ('https://h/?a=1&r=https://' + 'h' * 600 + '/p?code=c-YYY000', 'c-YYY000'),
    ('token eyJhbGciOiJSUzI1NiJ9.' + 'Q' * 5000 + '.sig-XXX000 rejected', 'sig-XXX000'),
    ('token eyJhbGciOiJSUzI1NiJ9' '.' 'eyJzdWIiOiJ4In0' '.' + 'S' * 4096 + 'TAIL-WWW000 rejected', 'TAIL-WWW000'),

    ('refresh failed: access_token=tok-AAA111 expired', 'tok-AAA111'),
    ('client_secret=cs-BBB222&grant_type=client_credentials', 'cs-BBB222'),
    ("bad config {'password': 'pw-CCC333', 'name': 'x'}", 'pw-CCC333'),
    ('bad body {"api_key": "ak-DDD444"}', 'ak-DDD444'),
    ('DB_PASSWORD="pw-EEE555" rejected', 'pw-EEE555'),
    ('GET https://h.example/cb?code=c-FFF666&state=s-GGG777 failed', 'c-FFF666'),
    ("login password='correct horse battery staple' failed", 'battery staple'),
    ("cfg {'password': 'prefix,SECRET_SUFFIX'}", 'SECRET_SUFFIX'),
    ('cfg {"client_secret": "a\\"b SECRET_ESC"}', 'SECRET_ESC'),
    ('dial from 2001:db8::7334 refused', '2001:db8::7334'),
    ('peer fe80::1ff:fe23:4567:890a closed', 'fe80::1ff'),
    ('bind ::1 ok', '::1'),
    ("cfg password='abc\\\nSECRET_NL tail' end", 'SECRET_NL'),
    ('cfg {"token": "x\\\ny SECRET_NL2"}', 'SECRET_NL2'),
    ('peer:2001:db8::1 closed', '2001:db8'),
    ('addr=fe80::1:2: refused', 'fe80::1'),
    ('dial [::1]:8080 failed', '::1'),
    ('connect host=db password=,SENSITIVE_VALUE dbname=x', 'SENSITIVE_VALUE'),
    ('connect host=db password=prefix,SECRET_SUFFIX dbname=x', 'SECRET_SUFFIX'),
    ('connect password=a\\ SECRET_SPACE dbname=x', 'SECRET_SPACE'),
    ('connect password=pre}SECRET_BRACE x', 'SECRET_BRACE'),
    ("connect password=pre'SECRET_QUOTE x", 'SECRET_QUOTE'),
    ('{"token":null,"password":"prefix SECRET_SUFFIX"}', 'SECRET_SUFFIX'),
    ("{'token': 1, 'client_secret': 'a b SECRET_TWO'}", 'SECRET_TWO'),
    ('password=x,api_key="k SECRET_THREE"', 'SECRET_THREE'),
    ('{"pwd":"","secret":"s SECRET_FOUR"}', 'SECRET_FOUR'),
    ("password=foo,token= sslpassword='prefix SECRET_SUFFIX' dbname=x", 'SECRET_SUFFIX'),
    ('{"url":"https://example.test/?x=1","password":"prefix SECRET_SUFFIX"}', 'SECRET_SUFFIX'),
    ('see https://h/p?token=t-AAA&next=1 and {"secret":"s b SECRET_B"}', 'SECRET_B'),
    ('{"token":"a b SECRET_C","url":"https://h/?q=1"}', 'SECRET_C'),
    ('token=token=token= password="x y SECRET_D"', 'SECRET_D'),
    ('cfg password=\\\'a "b" c SECRET_EQ\\\' end', 'SECRET_EQ'),
    ('cfg password=\\"a \'b\' c SECRET_EQ\\" end', 'SECRET_EQ'),
]


@pytest.mark.parametrize('message, secret', _SECRET_MESSAGES)
def test_no_exception_text_leaves_the_process(monkeypatch, message, secret):
    events: list[dict[str, Any]] = []

    class _Memory(sentry_sdk.transport.Transport):
        def capture_envelope(self, envelope):
            events.extend(item.payload.json for item in envelope.items if item.type == 'event')

    real_init = sentry_sdk.init
    monkeypatch.setattr(sentry_sdk, 'init', lambda **kw: real_init(transport=_Memory, **kw))
    monkeypatch.setenv('SENTRY_DSN', 'https://k@o0.ingest.sentry.io/1')
    _sentry.init_sentry()
    try:
        try:
            raise ValueError(message)
        except ValueError as exc:
            sentry_sdk.capture_exception(exc)
        sentry_sdk.capture_message(message)
        sentry_sdk.add_breadcrumb(message=message)
        sentry_sdk.capture_exception(RuntimeError('after breadcrumb'))
        sentry_sdk.flush()
    finally:
        real_init(dsn='')
    assert len(events) == 3
    for event in events:
        assert secret not in str(event)
    assert events[0]['exception']['values'][-1]['type'] == 'ValueError'


def test_capture_errors_skips_building_an_event_without_a_transport(monkeypatch):
    # Building the event reads traceback locals; with nothing to send it must not happen at all.
    import asyncio

    import sentry_sdk.utils

    sentry_sdk.init(dsn='')
    built = []
    monkeypatch.setattr(sentry_sdk.utils, 'event_from_exception', lambda *a, **kw: built.append(a))

    async def broken(scope, receive, send):
        raise RuntimeError('boom')

    with pytest.raises(RuntimeError):
        asyncio.run(_sentry.CaptureErrors(broken)({}, None, None))
    assert built == []
