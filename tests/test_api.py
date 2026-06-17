"""Standalone client regression tests; no Home Assistant runtime required."""
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def api():
    root = Path(__file__).parents[1] / 'custom_components' / 'hero'
    modules = {}
    for name in ('homeassistant', 'homeassistant.config_entries', 'homeassistant.const',
                 'homeassistant.core', 'homeassistant.helpers',
                 'homeassistant.helpers.aiohttp_client', 'hero_test'):
        modules[name] = ModuleType(name)
    modules['homeassistant.config_entries'].ConfigEntry = object
    modules['homeassistant.core'].HomeAssistant = object
    modules['homeassistant.const'].CONF_USERNAME = 'username'
    modules['homeassistant.const'].CONF_PASSWORD = 'password'
    modules['homeassistant.helpers.aiohttp_client'].async_create_clientsession = lambda *a, **kw: None
    modules['hero_test'].__path__ = [str(root)]
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location('hero_test.api', root / 'api.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module


def test_navigation_ignores_favicon_and_decodes_anchor(api):
    assert api._html_href('<link href="/favicon.ico">') is None
    assert api._html_href('<link href="/favicon.ico"><a href="/next?a=1&amp;b=2">Go</a>') == '/next?a=1&b=2'
    assert api._html_href('<script>window.location.replace("/next")</script>') == '/next'


def test_only_app_callback_code_is_accepted(api):
    async def run():
        client = object.__new__(api.HeroClient)
        client._request = AsyncMock(side_effect=api.HeroApiError('unexpected request'))
        assert await client._follow_to_code('https://our.linc-ed.com/callback?code=test') == 'test'
        with pytest.raises(api.HeroApiError):
            await client._follow_to_code('https://example.com/callback?code=test')
        client._request.assert_not_called()
    asyncio.run(run())


def test_login_preserves_server_identity_challenge(api):
    async def run():
        client = object.__new__(api.HeroClient)
        form = api._parse_first_form('<form action="/oauth2/authorize"><input name="code_challenge" value="server-challenge"><input name="state" value="server-state"></form>')
        client._request = AsyncMock(side_effect=[SimpleNamespace(url='https://id.linc-ed.com/oauth2/authorize'), api.HeroApiError('stop after identifier')])
        client._form_from_response = AsyncMock(return_value=form)
        with pytest.raises(api.HeroApiError, match='stop after identifier'):
            await client.authenticate('test@example.com', 'test-password', 'tenant', 'Pacific/Auckland')
        first, second = client._request.call_args_list
        assert 'params' not in first.kwargs
        assert second.kwargs['data']['code_challenge'] == 'server-challenge'
        assert second.kwargs['data']['state'] == 'server-state'
    asyncio.run(run())


def test_calendar_is_discovered_from_school_options(api):
    async def run():
        client = object.__new__(api.HeroClient)
        client.entry = SimpleNamespace(data={'school_ids': ['school-example']})
        client.ensure_token = AsyncMock(return_value='test-token')
        client._hero_get = AsyncMock(return_value={'school': {'name': 'Example school', 'options': {'app:links:googleCalendar': {'valueString': 'example@group.calendar.google.com'}}}})
        assert await client.async_get_school_calendar_id() == ('Example school', 'example@group.calendar.google.com')
        client._hero_get.assert_awaited_once_with('/schools/v4/schools/school-example', 'test-token')
    asyncio.run(run())


def test_identity_cookie_does_not_skip_verification(api):
    async def run():
        client = object.__new__(api.HeroClient)
        callback = SimpleNamespace(
            headers={'Content-Type': 'text/html'},
            cookies={'hero.fa.at': 'test-cookie'},
            charset='utf-8',
            read=AsyncMock(return_value=b'<script>window.location.replace("/verify")</script>'),
        )
        verify = SimpleNamespace(
            headers={'Content-Type': 'text/html'}, cookies={}, charset='utf-8',
            read=AsyncMock(return_value=b'<script>window.location.replace("https://our.linc-ed.com")</script>'),
        )
        client._request = AsyncMock(side_effect=[callback, verify])
        await client._complete_identity_login('/callback?code=identity-test', 'test@example.com', 'test-password')
        assert [call.args[1] for call in client._request.call_args_list] == [
            'https://id.linc-ed.com/callback?code=identity-test',
            'https://id.linc-ed.com/verify',
        ]
    asyncio.run(run())


def test_google_key_loaded_from_app_config_and_cached(api):
    async def run():
        client = object.__new__(api.HeroClient)
        client._google_api_key = None
        content = api.quote('{"googleApiKey":"test-runtime-key"}', safe='')
        response = SimpleNamespace(
            charset='utf-8', headers={'Content-Type': 'text/html'},
            read=AsyncMock(return_value=(
                f'<meta content="{content}" name="linced-parents/config/environment" />'
            ).encode()),
        )
        client._request = AsyncMock(return_value=response)
        client._json_request = AsyncMock(return_value={'items': [{'id': 'event'}]})
        now = api.datetime.now()
        assert await client.async_get_google_events('calendar', now, now) == [{'id': 'event'}]
        assert client._json_request.call_args.kwargs['params']['key'] == 'test-runtime-key'
        assert await client._get_google_api_key() == 'test-runtime-key'
        client._request.assert_awaited_once_with(
            'GET', api.APP_REFERER, headers=client._app_authorize_headers()
        )
    asyncio.run(run())


@pytest.mark.parametrize('content', ['', '%7Bbroken', '%7B%7D', '%5B%5D',
                                     '%7B%22googleApiKey%22%3Anull%7D',
                                     '%7B%22googleApiKey%22%3A%22%20%22%7D'])
def test_google_key_rejects_missing_or_invalid_config(api, content):
    async def run():
        client = object.__new__(api.HeroClient)
        client._google_api_key = None
        client._request = AsyncMock(return_value=SimpleNamespace(
            charset='utf-8', headers={'Content-Type': 'text/html'}, read=AsyncMock(return_value=(
                f'<meta name="linced-parents/config/environment" content="{content}">'
            ).encode()),
        ))
        client._json_request = AsyncMock()
        with pytest.raises(api.HeroApiError, match='Hero .*configuration'):
            await client.async_get_google_events('calendar', api.datetime.now(), api.datetime.now())
        client._json_request.assert_not_called()
        assert client._google_api_key is None
    asyncio.run(run())
