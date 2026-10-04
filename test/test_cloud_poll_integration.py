# -*- coding: utf-8 -*-
"""Offline cloud-poll integration tests using production modules and mock I/O.

HA runtime imports are placeholders. The multi_select validator is loaded from
the real HA source distribution, without importing its service dependencies.
Frontend rendering and entity state writes still require Home Assistant.
"""
import ast
import asyncio
import importlib
from importlib.metadata import distribution
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
import pytest_asyncio
import voluptuous as vol

# pylint: disable=protected-access, import-outside-toplevel
# pylint: disable=redefined-outer-name

pytestmark = [pytest.mark.github, pytest.mark.asyncio]
INTEGRATION_PATH = (
    Path(__file__).resolve().parents[1] / 'custom_components' / 'xiaomi_home')


class ImportOnlyConfigFlow:
    """Accept HA's domain keyword while importing the actual config flow."""

    def __init_subclass__(cls, **_kwargs):
        super().__init_subclass__()


@pytest.fixture
def ha_multi_select():
    """Compile HA's original standalone validator without reimplementing it."""
    path = distribution('homeassistant').locate_file(
        'homeassistant/helpers/config_validation.py')
    tree = ast.parse(path.read_text(encoding='utf-8'))
    validator = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == 'multi_select')
    namespace = {'vol': vol}
    exec(  # pylint: disable=exec-used
        compile(ast.Module(body=[validator], type_ignores=[]),
                str(path), 'exec'),
        namespace)
    return namespace['multi_select']


@pytest.fixture
def integration_modules(monkeypatch, ha_multi_select):
    """Load whole source modules, mocking only unused HA import interfaces."""
    modules = {
        'homeassistant': {},
        'homeassistant.core': {
            'HomeAssistant': object, 'callback': lambda func: func},
        'homeassistant.config_entries': {
            'ConfigEntry': object, 'ConfigFlow': ImportOnlyConfigFlow,
            'OptionsFlow': object},
        'homeassistant.components': {},
        'homeassistant.components.zeroconf': {'HaAsyncZeroconf': object},
        'homeassistant.components.webhook': {
            'async_register': Mock(), 'async_unregister': Mock(),
            'async_generate_path': Mock()},
        'homeassistant.components.persistent_notification': {},
        'homeassistant.data_entry_flow': {'AbortFlow': Exception},
        'homeassistant.helpers': {},
        'homeassistant.helpers.instance_id': {'async_get': AsyncMock()},
        'homeassistant.helpers.config_validation': {
            'config_entry_only_config_schema': Mock(),
            'multi_select': ha_multi_select},
        'homeassistant.helpers.device_registry': {},
        'homeassistant.helpers.entity_registry': {},
    }
    for name, attributes in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
        if '.' in name:
            parent, _, child = name.rpartition('.')
            setattr(sys.modules[parent], child, module)

    # Use a private package to load the entry point after mocking HA imports,
    # without affecting other tests' miot imports.
    package_name = '_cloud_poll_test'
    package = ModuleType(package_name)
    package.__path__ = [str(INTEGRATION_PATH)]
    monkeypatch.setitem(sys.modules, package_name, package)
    device_module = ModuleType(f'{package_name}.miot.miot_device')
    device_module.MIoTDevice = object
    monkeypatch.setitem(sys.modules, device_module.__name__, device_module)
    try:
        client = importlib.import_module(f'{package_name}.miot.miot_client')
        flow = importlib.import_module(f'{package_name}.config_flow')
        spec = importlib.util.spec_from_file_location(
            package_name, INTEGRATION_PATH / '__init__.py',
            submodule_search_locations=[str(INTEGRATION_PATH)])
        spec.loader.exec_module(package)
        yield SimpleNamespace(client=client, flow=flow, integration=package)
    finally:
        for name in list(sys.modules):
            if name.startswith(f'{package_name}.'):
                sys.modules.pop(name, None)


async def test_import_stubs_are_scoped(ha_multi_select):
    before = {name: module for name, module in sys.modules.items()
              if name == 'homeassistant' or name.startswith('homeassistant.')}
    with pytest.MonkeyPatch.context() as patch:
        loader = integration_modules.__wrapped__(patch, ha_multi_select)
        try:
            next(loader)
            assert sys.modules['homeassistant'] is not before.get(
                'homeassistant')
        finally:
            loader.close()
    after = {name: module for name, module in sys.modules.items()
             if name == 'homeassistant' or name.startswith('homeassistant.')}
    assert after == before
    assert not any(name.startswith('_cloud_poll_test') for name in sys.modules)


@pytest.fixture
def cloud_client(integration_modules):
    """Instantiate the real client without starting its network services."""
    module = integration_modules.client
    client = module.MIoTClient.__new__(module.MIoTClient)
    client._network = SimpleNamespace(network_status=True)
    client._sub_tree = module.MIoTMatcher()
    client._device_list_cache = {'selected': {}}
    client._refresh_props_list = {}
    client._http = module.MIoTHttpClient.__new__(module.MIoTHttpClient)
    client._http._MIoTHttpClient__mihome_api_post_async = AsyncMock()
    return client


def prop(piid, **fields):
    """A synthetic property request or response."""
    return {'did': 'selected', 'siid': 2, 'piid': piid, **fields}


async def test_helper_dispatches_cloud_values_and_filters_errors(cloud_client):
    responses = [
        prop(1, value=False, code=0), prop(2, value=0),
        prop(3, value='', code=0), prop(4, value=None),
        prop(5, value=42, code=-1), prop(6, code=-1),
        {'did': 'selected', 'value': 12}, prop(99, value=99),
    ]
    post = cloud_client._http._MIoTHttpClient__mihome_api_post_async
    post.return_value = {'result': responses}
    handler = Mock()
    cloud_client.sub_prop('selected', handler, handler_ctx='entity-context')
    params = [prop(piid) for piid in range(1, 7)]

    refreshed = await cloud_client.refresh_cloud_props_async(params)

    assert refreshed == {f'selected|2|{piid}' for piid in range(1, 5)}
    assert handler.call_args_list == [
        call(result, 'entity-context') for result in responses[:4]]
    post.assert_awaited_once_with(
        url_path='/app/v2/miotspec/prop/get',
        data={'datasource': 1, 'params': params})


@pytest.mark.parametrize('offline', [False, True])
async def test_helper_skips_empty_or_offline_requests(cloud_client, offline):
    cloud_client._network.network_status = not offline
    assert await cloud_client.refresh_cloud_props_async(
        [prop(1)] if offline else []) == set()
    post = cloud_client._http._MIoTHttpClient__mihome_api_post_async
    post.assert_not_awaited()


@pytest.fixture
def http_transport(cloud_client):
    """Mock the socket boundary, retaining the real HTTP response checks."""
    http = cloud_client._http
    del http._MIoTHttpClient__mihome_api_post_async
    http._host = 'example.invalid'
    http._base_url = 'https://example.invalid'
    http._access_token = 'synthetic-test-value'
    http._client_id = 'test-client'
    http._user_agent = 'offline-test'
    http._session = SimpleNamespace(post=AsyncMock())
    return http._session.post


@pytest.mark.parametrize('status,body', [
    (401, {}), (429, {}), (200, {'code': -1}),
    (200, {'code': 0}), (200, {'code': 0, 'result': []})])
async def test_helper_http_failures_do_not_dispatch(
    cloud_client, http_transport, status, body
):
    errors = importlib.import_module('_cloud_poll_test.miot.miot_error')
    http_transport.return_value = SimpleNamespace(
        status=status, text=AsyncMock(return_value=json.dumps(body)))
    handler = Mock()
    cloud_client.sub_prop('selected', handler)

    with pytest.raises(errors.MIoTError) as caught:
        await cloud_client.refresh_cloud_props_async([prop(1)])

    if status == 401:
        assert caught.value.code == (
            errors.MIoTErrorCode.CODE_HTTP_INVALID_ACCESS_TOKEN)
    http_transport.assert_awaited_once()
    handler.assert_not_called()


async def test_poller_stop_cancels_real_helper_http_request(
    integration_modules, cloud_client, http_transport
):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def post(**_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    http_transport.side_effect = post
    cloud_client._main_loop = asyncio.get_running_loop()
    device = SimpleNamespace(
        did='selected', entity_list={}, prop_list={'sensor': [
            SimpleNamespace(
                readable=True, iid=1, service=SimpleNamespace(iid=2))]})
    poller = integration_modules.integration.MIoTCloudPoller(
        cloud_client, [device], {'selected'})
    handler = Mock()
    cloud_client.sub_prop('selected', handler)
    poller.start()
    poller._timer.cancel()
    poller._MIoTCloudPoller__start_poll()
    task = poller._task
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.wait_for(poller.stop(), timeout=5)
        assert task.cancelled()
        assert cancelled.is_set()
        assert poller._timer is None
        assert not poller.active
        http_transport.assert_awaited_once()
        handler.assert_not_called()
    finally:
        await poller.stop()


@pytest.mark.parametrize('outcome', ['complete', 'partial', 'empty', 'error'])
async def test_old_cloud_refresh_queue_behavior(
    integration_modules, cloud_client, outcome
):
    pending = {f'selected|2|{piid}': prop(piid) for piid in range(1, 3)}
    cloud_client._refresh_props_list = pending.copy()
    handler = Mock()
    cloud_client.sub_prop('selected', handler)
    post = cloud_client._http._MIoTHttpClient__mihome_api_post_async
    if outcome == 'error':
        post.side_effect = integration_modules.client.MIoTClientError('failed')
    else:
        post.return_value = {'result': {
            'complete': [prop(1, value=0), prop(2, value=False)],
            'partial': [prop(1, value=0), prop(2, code=-1)],
            'empty': [],
        }[outcome]}

    succeeded = await cloud_client._MIoTClient__refresh_props_from_cloud()

    if outcome in ['empty', 'error']:
        assert not succeeded
        assert cloud_client._refresh_props_list == pending
        handler.assert_not_called()
    else:
        assert succeeded
        assert cloud_client._refresh_props_list == {}
        assert handler.call_count == (2 if outcome == 'complete' else 1)


@pytest_asyncio.fixture
async def options_flow(integration_modules):
    entry = SimpleNamespace(
        entry_id='entry', unique_id='test-entry', options={}, data={
            'virtual_did': 'test-entry', 'uid': 'test-user',
            'storage_path': 'unused', 'cloud_server': 'cn', 'uuid': 'test-uuid',
            'home_selected': {},
            'cloud_poll_device_ids': ['selected', 'removed'],
            'cloud_poll_interval': 60})
    flow = integration_modules.flow.OptionsFlowHandler(entry)
    flow._main_loop = Mock(spec=asyncio.AbstractEventLoop)
    flow._main_loop.create_task.side_effect = asyncio.create_task
    flow._miot_client = SimpleNamespace(device_list={
        did: {'home_name': 'Home', 'room_name': 'Room', 'name': did}
        for did in ['selected', 'removed']})
    flow._miot_storage = SimpleNamespace(
        save_async=AsyncMock(return_value=True),
        update_user_config_async=AsyncMock(return_value=True))
    flow.hass = SimpleNamespace(config_entries=SimpleNamespace(
        async_update_entry=Mock(), async_reload=AsyncMock()))
    flow.async_show_form = Mock(side_effect=lambda **kwargs: kwargs)
    flow.async_create_entry = Mock(return_value={'type': 'create_entry'})
    # These normally come from the preceding config_options step.
    flow._display_binary_mode_new = flow._display_binary_mode
    flow._opt_cloud_poll_cfg = True
    return flow


@pytest.mark.parametrize('selection,interval', [
    (None, 60), ([], 60), (['selected'], 120), (['selected', 'removed'], 120)])
async def test_options_selection_save_and_reload(
    options_flow, selection, interval
):
    flow = options_flow
    # Stop at the next page until the user explicitly confirms saving.
    flow.async_step_update_lan_ctrl_config = AsyncMock()
    form = await flow.async_step_cloud_poll_config()
    assert form['step_id'] == 'cloud_poll_config'
    assert form['description_placeholders'] == {
        'min_interval': '30', 'max_interval': '3600'}
    submitted = {'cloud_poll_interval': interval}
    if selection is not None:
        submitted['cloud_poll_device_ids'] = selection
    data = form['data_schema'](submitted)
    await flow.async_step_cloud_poll_config(data)
    flow.hass.config_entries.async_update_entry.assert_not_called()
    await flow.async_step_config_confirm({'confirm': True})

    saved = flow.hass.config_entries.async_update_entry.call_args.kwargs['data']
    assert saved['cloud_poll_device_ids'] == (selection or [])
    assert saved.get('cloud_poll_interval', 60) == interval
    flow._main_loop.call_later.assert_called_once()
    delay, callback = flow._main_loop.call_later.call_args.args
    assert delay == 0
    await callback()
    flow.hass.config_entries.async_reload.assert_awaited_once_with(
        entry_id='entry')


async def test_options_real_validator_rejects_unknown_devices(options_flow):
    flow = options_flow
    form = await flow.async_step_cloud_poll_config()
    schema = form['data_schema']
    for selection in [['not-imported'], 'selected', None]:
        with pytest.raises(vol.Invalid):
            schema({'cloud_poll_device_ids': selection})
    for interval in [29, 3601]:
        with pytest.raises(vol.Invalid):
            schema({'cloud_poll_interval': interval})
    marker = next(
        key for key in schema.schema if key == 'cloud_poll_device_ids')
    assert marker.description['suggested_value'] == ['selected', 'removed']
    assert 'cloud_poll_device_ids' not in schema({})


async def test_ci_slugify_supports_integration_call(integration_modules):
    assert integration_modules.client.slugify_did(
        'cn', 'Test Device') == 'cn_test_device'


async def test_device_update_prunes_dids_without_opening_poll_options(
    options_flow
):
    flow = options_flow
    flow._opt_cloud_poll_cfg = False
    flow._update_devices = True
    flow._device_list_sorted = {
        'selected': flow._miot_client.device_list['selected']}
    flow._devices_remove = ['removed']
    flow.async_step_update_lan_ctrl_config = AsyncMock()
    await flow.async_step_cloud_poll_config()
    flow.async_show_form.assert_not_called()
    await flow.async_step_config_confirm({'confirm': True})

    saved = flow.hass.config_entries.async_update_entry.call_args.kwargs['data']
    assert saved['cloud_poll_device_ids'] == ['selected']
    flow._miot_storage.save_async.assert_awaited_once_with(
        domain='miot_devices', name='test-user_cn',
        data=flow._device_list_sorted)
    flow._miot_storage.update_user_config_async.assert_awaited_once_with(
        uid='test-user', cloud_server='cn',
        config={'devices_remove': ['removed']})
    await flow._main_loop.call_later.call_args.args[1]()
    flow.hass.config_entries.async_reload.assert_awaited_once_with(
        entry_id='entry')


@pytest.mark.parametrize('unload_ok', [False, True])
async def test_unload_stops_poller_and_restarts_only_on_failure(
    integration_modules, unload_ok
):
    integration = integration_modules.integration
    client = SimpleNamespace(
        main_loop=asyncio.get_running_loop(),
        refresh_cloud_props_async=AsyncMock(), deinit_async=AsyncMock())
    device = SimpleNamespace(
        did='selected', entity_list={}, prop_list={'sensor': [
            SimpleNamespace(
                readable=True, iid=1, service=SimpleNamespace(iid=2))]})
    poller = integration.MIoTCloudPoller(client, [device], {'selected'})
    poller.start()
    first_timer = poller._timer
    entry_data = {
        'cloud_pollers': {'entry': poller}, 'miot_clients': {'entry': client},
        'devices': {'entry': [device]}, 'entities': {'entry': []}}

    async def unload(entry, platforms):
        assert entry.entry_id == 'entry'
        assert platforms == integration.SUPPORTED_PLATFORMS
        assert not poller.active
        assert first_timer.cancelled()
        client.deinit_async.assert_not_awaited()
        return unload_ok

    hass = SimpleNamespace(
        data={integration.DOMAIN: entry_data},
        config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(
            side_effect=unload)))
    try:
        assert await integration.async_unload_entry(
            hass, SimpleNamespace(entry_id='entry')) is unload_ok
        if unload_ok:
            assert all('entry' not in items for items in entry_data.values())
            assert not poller.active
            assert poller._timer is None
            client.deinit_async.assert_awaited_once()
        else:
            assert all('entry' in items for items in entry_data.values())
            assert poller.active
            assert poller._timer is not first_timer
            client.deinit_async.assert_not_awaited()
    finally:
        await poller.stop()


@pytest.mark.parametrize('mode', [
    'default', 'cleared', 'selected', 'unreadable', 'setup_error'])
async def test_setup_registers_poller_only_after_platform_setup(
    integration_modules, monkeypatch, mode
):
    integration = integration_modules.integration
    storage = SimpleNamespace(load_user_config_async=AsyncMock(return_value={}))
    client = SimpleNamespace(
        miot_storage=storage, main_loop=asyncio.get_running_loop(),
        device_list={'selected': {'urn': 'test:device'}},
        hide_non_standard_entities=False, action_debug=False,
        display_binary_bool=True, display_binary_text=True,
        refresh_cloud_props_async=AsyncMock())
    parser = SimpleNamespace(
        init_async=AsyncMock(), deinit_async=AsyncMock(),
        parse=AsyncMock(return_value=Mock(spec=integration.MIoTSpecInstance)))
    manufacturer = SimpleNamespace(
        init_async=AsyncMock(), deinit_async=AsyncMock(), get_name=Mock())
    device = SimpleNamespace(
        did='selected', entity_list={}, prop_list={}, event_list={},
        action_list={})

    def transform():
        device.prop_list['sensor'] = [SimpleNamespace(
            readable=mode != 'unreadable', need_filter=False, proprietary=False,
            iid=1, service=SimpleNamespace(iid=2))]

    device.spec_transform = Mock(side_effect=transform)
    monkeypatch.setattr(integration, 'get_miot_instance_async', AsyncMock(
        return_value=client))
    monkeypatch.setattr(integration, 'MIoTSpecParser',
                        Mock(return_value=parser))
    monkeypatch.setattr(integration, 'DeviceManufacturer', Mock(
        return_value=manufacturer))
    monkeypatch.setattr(integration, 'MIoTDevice', Mock(return_value=device))
    monkeypatch.setattr(integration.entity_registry, 'async_get', Mock(),
                        raising=False)
    monkeypatch.setattr(integration.entity_registry,
                        'async_entries_for_config_entry', Mock(return_value=[]),
                        raising=False)
    monkeypatch.setattr(integration.persistent_notification, 'async_dismiss',
                        Mock(), raising=False)
    hass = SimpleNamespace(data={}, config_entries=SimpleNamespace())
    await integration.async_setup(hass, {})
    pollers = hass.data[integration.DOMAIN]['cloud_pollers']

    async def forward(entry, platforms):
        assert entry.entry_id == 'entry'
        assert platforms == integration.SUPPORTED_PLATFORMS
        assert not pollers
        device.spec_transform.assert_called_once()
        if mode == 'setup_error':
            raise RuntimeError('platform setup failed')

    hass.config_entries.async_forward_entry_setups = AsyncMock(
        side_effect=forward)
    data = {'uid': 'test-user', 'cloud_server': 'cn'}
    if mode != 'default':
        data['cloud_poll_device_ids'] = (
            [] if mode == 'cleared' else ['selected'])
    entry = SimpleNamespace(entry_id='entry', data=data)
    try:
        if mode == 'setup_error':
            with pytest.raises(RuntimeError, match='platform setup failed'):
                await integration.async_setup_entry(hass, entry)
        else:
            assert await integration.async_setup_entry(hass, entry)
        client.refresh_cloud_props_async.assert_not_awaited()
        if mode == 'selected':
            assert pollers['entry'].active
            assert pollers['entry'].prop_count == 1
        else:
            assert not pollers
    finally:
        for poller in pollers.values():
            await poller.stop()
