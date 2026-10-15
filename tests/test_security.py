"""Offline regression tests. No vehicle requests or credentials are used."""
import asyncio
import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock

# Load the API without importing Home Assistant's integration entrypoint.
pkg = types.ModuleType('hondalink_test_package')
pkg.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[pkg.__name__] = pkg
api_module = importlib.import_module(pkg.__name__ + '.api')
util = importlib.import_module(pkg.__name__ + '.util')
API = api_module.HondaLinkAPI
Error = api_module.HondaLinkError
AuthError = api_module.HondaLinkAuthError

class Response:
    def __init__(self, status=200, body='{}'):
        self.status = status
        self.body = body
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def text(self):
        return self.body

class Session:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []
    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        item = next(self.responses)
        if isinstance(item, Exception):
            raise item
        return item

def client(session=None):
    return API(session or Session(), email='test@example.invalid', password='secret', pin='1234')

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_sensitive_body_never_in_errors(self):
        for response in [Response(500, '{"errorMessage":"secret VIN GPS pin=1234"}'), Response(200, '<html>secret</html>'), Response(401, 'secret')]:
            with self.subTest(status=response.status):
                with self.assertRaises(Error) as caught:
                    await client(Session(response))._send('GET', 'https://example.invalid', headers={})
                self.assertNotIn('secret', str(caught.exception))
                self.assertNotIn('1234', str(caught.exception))

    async def test_timeout_and_redirect_protection(self):
        session = Session(Response(307))
        with self.assertRaisesRegex(Error, 'redirect refused'):
            await client(session)._send('POST', 'https://example.invalid', headers={}, data={'password':'secret'})
        options = session.calls[0][1]
        self.assertFalse(options['allow_redirects'])
        self.assertEqual(options['timeout'].total, 30)
        self.assertNotIn('ssl', options)  # aiohttp certificate verification remains enabled
        self.assertEqual(len(session.calls), 1)

    async def test_transport_error_is_sanitized(self):
        for error in [TimeoutError('secret'), api_module.aiohttp.ClientError('secret')]:
            with self.assertRaisesRegex(Error, '^HondaLink connection failed or timed out$'):
                await client(Session(error))._send('GET', 'https://example.invalid', headers={})

    async def test_non_object_json_rejected(self):
        for body in ['[]', 'null', '"secret"', '42']:
            with self.assertRaisesRegex(Error, 'response structure'):
                await client(Session(Response(body=body)))._send('GET', 'https://example.invalid', headers={})

    async def test_http_auth_status_preserved(self):
        for status in [401, 403]:
            with self.assertRaises(AuthError) as caught:
                await client(Session(Response(status, 'not-json-secret')))._send('GET', 'https://example.invalid', headers={})
            self.assertEqual(caught.exception.status, status)

class AuthTests(unittest.IsolatedAsyncioTestCase):
    def make_api(self, outcomes):
        api = client()
        api.async_ensure_login = AsyncMock()
        api.async_login = AsyncMock()
        api._send = AsyncMock(side_effect=outcomes)
        api.access_token = 'old'
        api.client_reg_key = 'old-registration'
        return api

    async def test_http_401_refreshes_registration_once(self):
        api = self.make_api([AuthError('rejected', status=401), {'status':'success'}])
        self.assertEqual(await api._request_api('GET','/test'), {'status':'success'})
        api.async_login.assert_awaited_once()
        self.assertIsNone(api.client_reg_key)
        self.assertEqual(api._send.await_count,2)

    async def test_mixed_401_does_not_retry_twice(self):
        api = self.make_api([AuthError('rejected',status=401), {'Header':{'ErrorCode':'401'}}])
        with self.assertRaises(AuthError):
            await api._request_api('POST','/test')
        self.assertEqual(api._send.await_count,2)
        api.async_login.assert_awaited_once()

    async def test_body_401_refreshes_once(self):
        api = self.make_api([{'Header':{'ErrorCode':401}}, {'status':'success'}])
        await api._request_api('GET','/test')
        api.async_login.assert_awaited_once()

    async def test_403_does_not_replay(self):
        api = self.make_api([AuthError('forbidden',status=403)])
        with self.assertRaises(AuthError):
            await api._request_api('POST','/test')
        api.async_login.assert_not_awaited()
        self.assertEqual(api._send.await_count,1)

    async def test_retry_false(self):
        api = self.make_api([{'Header':{'ErrorCode':'401'}}])
        with self.assertRaises(AuthError):
            await api._request_api('GET','/test',retry=False)
        api.async_login.assert_not_awaited()

    async def test_ambiguous_network_failure_never_replays_command(self):
        api = self.make_api([Error('connection failed or timed out')])
        with self.assertRaises(Error):
            await api._async_cig_command('eng','srt',{})
        self.assertEqual(api._send.await_count,1)

    async def test_concurrent_login_is_serialized(self):
        api = client()
        async def login():
            await asyncio.sleep(0)
            api.access_token = 'new'
            api.expires_at = api_module.time.time()+1000
            api.hidas_ident = 'user'
        api.async_login = AsyncMock(side_effect=login)
        await asyncio.gather(*(api.async_ensure_login() for _ in range(10)))
        api.async_login.assert_awaited_once()

class ErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_error_drops_remote_message(self):
        api = client()
        api._request_api = AsyncMock(return_value={'status':'failed','responseBody':{'errorMessage':'secret'}})
        with self.assertRaises(Error) as caught:
            await api._async_cig_command('lk','alk',{})
        self.assertNotIn('secret', str(caught.exception))

    async def test_poll_error_drops_remote_message(self):
        api = client()
        api._request_api = AsyncMock(return_value={'status':'failed','responseBody':{'errorMessage':'secret'}})
        with self.assertRaises(Error) as caught:
            await api._poll_cig_result('lk','request',1,0)
        self.assertNotIn('secret', str(caught.exception))

    def test_summary_only_allows_known_error_codes(self):
        payload = {'Header':{'ErrorCode':'0001-01-1150','ErrorDescription':'secret'}, 'VIN':'secret','gps':{'latitude':123}}
        summary=api_module._redact_payload(payload)
        self.assertIn('0001-01-1150',summary)
        self.assertNotIn('secret',summary)
        self.assertNotIn('123',summary)
        self.assertNotIn('secret',api_module._redact_payload({'errorCode':'secret'}))

class CommandResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_progress_submission_waits_for_completion_without_resending(self):
        api = client()
        api._request_api = AsyncMock(side_effect=[
            {"status":"in_progress", "responseBody":{"cigServiceRequestId":"test-request"}},
            {"status":"success", "responseBody":{"commandStatus":"in_progress"}},
            {"status":"success", "responseBody":{"commandStatus":"completed"}},
        ])
        result = await api._async_cig_command('eng', 'srt', {}, poll_interval=0)
        self.assertEqual(result.response['responseBody']['commandStatus'], 'completed')
        self.assertEqual([c.args[0] for c in api._request_api.await_args_list], ['POST','GET','GET'])

    async def test_success_envelope_does_not_hide_pending_command(self):
        api = client()
        api._request_api = AsyncMock(side_effect=[
            {"status":"success", "responseBody":{"commandStatus":"in_progress"}},
            {"status":"success", "responseBody":{"commandStatus":"completed"}},
        ])
        result = await api._poll_cig_result('eng','test-request',1,0)
        self.assertEqual(result['responseBody']['commandStatus'],'completed')
        self.assertEqual(api._request_api.await_count,2)

    async def test_success_envelope_does_not_hide_failed_command(self):
        api = client()
        api._request_api = AsyncMock(return_value={
            "status":"success", "responseBody":{"commandStatus":"failed","errorCode":"0001-01-1151","errorMessage":"private-password"}
        })
        with self.assertRaisesRegex(Error, 'Command result failed') as caught:
            await api._poll_cig_result('eng','test-request',1,0)
        self.assertIn('0001-01-1151',str(caught.exception))
        self.assertNotIn('private-password',str(caught.exception))

    async def test_pending_without_request_id_is_not_success(self):
        api=client()
        api._request_api=AsyncMock(return_value={"status":"in_progress","responseBody":{}})
        with self.assertRaisesRegex(Error,'completion unconfirmed'):
            await api._async_cig_command('eng','srt',{})
        self.assertEqual(api._request_api.await_count,1)

    async def test_empty_response_is_not_success(self):
        api=client()
        api._request_api=AsyncMock(return_value={})
        with self.assertRaisesRegex(Error,'no result details'):
            await api._async_cig_command('eng','srt',{})
        self.assertEqual(api._request_api.await_count,1)

    async def test_submission_failure_has_stage_and_safe_diagnostics(self):
        api=client()
        api._request_api=AsyncMock(return_value={"status":"failed","responseBody":{"errorCode":"0x01130010","errorMessage":"private-pin"}})
        with self.assertRaisesRegex(Error,'Command submission rejected') as caught:
            await api._async_cig_command('eng','srt',{})
        self.assertIn('0x01130010',str(caught.exception))
        self.assertNotIn('private-pin',str(caught.exception))

    async def test_body_status_used_when_command_status_absent(self):
        api=client()
        api._request_api=AsyncMock(return_value={"status":"success","responseBody":{"status":"failed"}})
        with self.assertRaisesRegex(Error,'Command result failed'):
            await api._poll_cig_result('eng','test-request',1,0)

    def test_untrusted_status_and_numeric_pin_are_not_logged(self):
        summary=api_module._redact_payload({"status":"private-token","errorCode":"1234","responseBody":{"commandStatus":"private-email@example.invalid"}})
        self.assertEqual(summary,'response details omitted')


class LockTests(unittest.TestCase):
    def body(self, *states):
        return {'doorStatus':{key:{'lockState':state} for key,state in zip(('firstRowDriver','firstRowPassenger','secondRowDriver','secondRowPassenger'), states)}}
    def test_all_locked(self):
        self.assertTrue(util.all_door_locks_locked(self.body('lock','lock','lock','lock')))
    def test_partial_locked_is_unknown(self):
        self.assertIsNone(util.all_door_locks_locked(self.body('lock')))
    def test_unknown_state_is_unknown(self):
        self.assertIsNone(util.all_door_locks_locked(self.body('lock','lock','unknown','lock')))
    def test_known_unlock_is_unlocked(self):
        self.assertFalse(util.all_door_locks_locked(self.body('unlock')))
    def test_missing_is_unknown(self):
        self.assertIsNone(util.all_door_locks_locked({}))

if __name__=='__main__':
    unittest.main()
