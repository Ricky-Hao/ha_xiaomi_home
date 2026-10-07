# -*- coding: utf-8 -*-
"""Offline cloud-poll integration tests using production modules and mock I/O.

HA runtime imports are placeholders. The multi_select validator is loaded from
the real HA source distribution, without importing its service dependencies.
Frontend rendering and entity state writes still require Home Assistant.
"""
import ast
import asyncio
from copy import deepcopy
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


class ImportOnlyAbortFlow(Exception):
    """Carry HA's abort details without importing its runtime dependencies."""

    def __init__(self, reason, description_placeholders=None):
        super().__init__(reason)
        self.reason = reason
        self.description_placeholders = description_placeholders or {}


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
        'homeassistant.data_entry_flow': {'AbortFlow': ImportOnlyAbortFlow},
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
    client = module.MIoTClient(
        entry_id='entry', entry_data={'uid': 'test-user', 'cloud_server': 'cn'},
        network=Mock(spec=module.MIoTNetwork),
        storage=Mock(spec=module.MIoTStorage),
        mips_service=Mock(spec=module.MipsService), miot_lan=Mock(),
        loop=Mock(spec=asyncio.AbstractEventLoop))
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


@pytest.mark.parametrize('malformed', [
    [None], [7], ['synthetic-private-response'], [['did', 'siid', 'piid']],
    [None, 7, 'did siid piid synthetic-private-response',
     ['did', 'siid', 'piid']]])
async def test_helper_ignores_malformed_items_before_valid_records(
    cloud_client, malformed, caplog
):
    caplog.set_level('DEBUG', logger=type(cloud_client).__module__)
    post = cloud_client._http._MIoTHttpClient__mihome_api_post_async
    valid = [prop(piid, value=value, code=0)
             for piid, value in enumerate([None, False, 0, ''], start=1)]
    post.return_value = {'result': [
        *malformed, *valid,
        prop(5, value='synthetic-private-response', code=-1), prop(6),
        {'value': 'synthetic-private-response'},
        prop(99, value='synthetic-private-response')]}
    handler = Mock()
    cloud_client.sub_prop('selected', handler)

    assert await cloud_client.refresh_cloud_props_async(
        [prop(piid) for piid in range(1, 7)]
    ) == {f'selected|2|{piid}' for piid in range(1, 5)}
    assert handler.call_args_list == [call(record, None) for record in valid]
    assert 'synthetic-private-response' not in caplog.text


@pytest.mark.parametrize('container', [
    None, 7, 'synthetic-private-response', {'unexpected': 'container'},
    prop(1, value=42), [], (prop(1, value=42),)])
async def test_helper_rejects_malformed_container(
    integration_modules, cloud_client, container, caplog
):
    post = cloud_client._http._MIoTHttpClient__mihome_api_post_async
    post.return_value = {'result': container}
    handler = Mock()
    cloud_client.sub_prop('selected', handler)
    with pytest.raises(integration_modules.client.MIoTClientError):
        await cloud_client.refresh_cloud_props_async([prop(1)])
    handler.assert_not_called()
    assert 'synthetic-private-response' not in caplog.text


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
async def options_flow(integration_modules, cloud_client):
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
    flow._miot_client = cloud_client
    cloud_client._device_list_cache = {
        did: {'home_name': 'Home', 'room_name': 'Room', 'name': did}
        for did in ['selected', 'removed']}
    flow._miot_storage = SimpleNamespace(
        save_async=AsyncMock(return_value=True),
        update_user_config_async=AsyncMock(return_value=True))
    flow.hass = SimpleNamespace(
        data={integration_modules.integration.DOMAIN: {
            'miot_clients': {'entry': cloud_client}}},
        config_entries=SimpleNamespace(
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
        entry_update_lock=asyncio.Lock(),
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


@pytest_asyncio.fixture
async def device_removal(integration_modules, cloud_client, monkeypatch):
    """Exercise the real HA removal entry point and both client removers."""
    integration = integration_modules.integration
    client = cloud_client
    client._main_loop = asyncio.get_running_loop()
    client._cloud_server = 'cn'
    client._uid = 'test-user'
    client._device_list_cache = {
        did: {} for did in ['blt.3.removed', 'z.kept', 'unselected']}
    client._sub_source_list = dict.fromkeys(client.device_list, 'cloud')
    client._mips_cloud = SimpleNamespace(unsub_prop=Mock(), unsub_event=Mock())
    client._display_devs_notify = False
    client._storage = SimpleNamespace(
        load_async=AsyncMock(side_effect=lambda **_: deepcopy(
            client.device_list)), save_async=AsyncMock(return_value=True))
    devices = [SimpleNamespace(
        did=did, entity_list={}, prop_list={'sensor': [SimpleNamespace(
            readable=True, iid=piid, service=SimpleNamespace(iid=2))
            for piid in range(1, 4)]}) for did in client.device_list]
    entry = SimpleNamespace(entry_id='entry', data={
        'cloud_poll_device_ids': ['blt.3.removed', 'z.kept'],
        'cloud_poll_interval': 120, 'unrelated': {'keep': True}})
    poller = integration.MIoTCloudPoller(
        client, devices, set(entry.data['cloud_poll_device_ids']),
        interval=120, batch_size=2)
    poller.start()
    registry = SimpleNamespace(async_remove_device=Mock())
    monkeypatch.setattr(integration.device_registry, 'async_get',
                        Mock(return_value=registry), raising=False)

    def update_entry(config_entry, *, data, title=None):
        assert config_entry is entry
        config_entry.data = data
        if title is not None:
            config_entry.title = title

    hass = SimpleNamespace(
        data={integration.DOMAIN: {
            'miot_clients': {'entry': client},
            'cloud_pollers': {'entry': poller}}},
        config_entries=SimpleNamespace(
            async_update_entry=Mock(side_effect=update_entry)))

    async def remove(did):
        tag = integration.slugify_did('cn', did)
        assert tag != did
        return await integration.async_remove_config_entry_device(
            hass, entry, SimpleNamespace(
                id='registry-id', identifiers={(integration.DOMAIN, tag)}))

    try:
        yield SimpleNamespace(
            client=client, poller=poller, entry=entry, hass=hass,
            registry=registry, remove=remove, devices=devices)
    finally:
        await poller.stop()


async def test_device_removal_prunes_polling_and_persisted_selection(
    integration_modules, device_removal
):
    ctx = device_removal
    original_data = ctx.entry.data
    post = ctx.client._http._MIoTHttpClient__mihome_api_post_async
    post.side_effect = lambda **kwargs: {'result': [
        {**param, 'value': 0} for param in kwargs['data']['params']]}
    for did, remaining in [('blt.3.removed', ['z.kept']), ('z.kept', [])]:
        assert await ctx.remove(did)
        assert ctx.entry.data == {
            **original_data, 'cloud_poll_device_ids': remaining}
        assert original_data['cloud_poll_device_ids'] == [
            'blt.3.removed', 'z.kept']
        assert ctx.poller.prop_count == 3 * len(remaining)
        assert ctx.poller.active == bool(remaining)
        ctx.client._mips_cloud.unsub_prop.assert_called_with(did=did)
        ctx.client._mips_cloud.unsub_event.assert_called_with(did=did)
        ctx.client._storage.save_async.assert_awaited_with(
            domain='miot_devices', name='test-user_cn',
            data=ctx.client.device_list)
        post.reset_mock()
        for _ in range(2):
            assert await ctx.poller.async_poll_once() == 3 * len(remaining)
        assert {param['did'] for request in post.await_args_list
                for param in request.kwargs['data']['params']} == set(remaining)
        # Even with the old device objects present, reload uses the saved list.
        reloaded = integration_modules.integration.MIoTCloudPoller(
            ctx.client, ctx.devices,
            set(ctx.entry.data['cloud_poll_device_ids']),
            interval=ctx.entry.data['cloud_poll_interval'])
        assert reloaded.prop_count == 3 * len(remaining)
    assert ctx.poller._timer is None
    assert ctx.registry.async_remove_device.call_count == 2
    assert ctx.hass.config_entries.async_update_entry.call_count == 2


@pytest.mark.parametrize('did', ['unselected', 'unknown'])
async def test_device_removal_unselected_or_unknown_is_polling_noop(
    device_removal, did
):
    ctx = device_removal
    original_data = ctx.entry.data
    timer = ctx.poller._timer
    assert await ctx.remove(did)
    assert ctx.entry.data is original_data
    ctx.hass.config_entries.async_update_entry.assert_not_called()
    assert ctx.poller.prop_count == 6
    assert ctx.poller.active
    assert ctx.poller._timer is timer
    assert ctx.client._storage.save_async.await_count == (did == 'unselected')


async def test_device_removal_cleans_selection_without_active_poller(
    integration_modules, device_removal
):
    ctx = device_removal
    await ctx.poller.stop()
    ctx.hass.data[integration_modules.integration.DOMAIN][
        'cloud_pollers'].clear()
    original_data = ctx.entry.data
    assert await ctx.remove('blt.3.removed')
    assert ctx.entry.data == {
        **original_data, 'cloud_poll_device_ids': ['z.kept']}
    ctx.hass.config_entries.async_update_entry.assert_called_once()


@pytest.mark.parametrize('last', [False, True])
async def test_device_removal_completes_while_request_in_flight(
    device_removal, last
):
    ctx = device_removal
    if last:
        assert await ctx.remove('z.kept')
    post = ctx.client._http._MIoTHttpClient__mihome_api_post_async
    started = asyncio.Event()
    release = asyncio.Event()

    async def response(**kwargs):
        started.set()
        await release.wait()
        return {'result': [{**param, 'value': False}
                           for param in kwargs['data']['params']]}

    post.side_effect = response
    ctx.poller._timer.cancel()
    ctx.poller._MIoTCloudPoller__start_poll()
    task = ctx.poller._task
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert await asyncio.wait_for(ctx.remove('blt.3.removed'), timeout=5)
        assert not task.done()
        assert ctx.entry.data['cloud_poll_device_ids'] == (
            [] if last else ['z.kept'])
        release.set()
        await asyncio.wait_for(task, timeout=5)
        assert [param for request in post.await_args_list[1:]
                for param in request.kwargs['data']['params']] == (
                    [] if last else [
                        {'did': 'z.kept', 'siid': 2, 'piid': piid}
                        for piid in range(1, 4)])
        assert ctx.poller.active is not last
        assert (ctx.poller._timer is None) is last
    finally:
        release.set()
        await ctx.poller.stop()


@pytest.mark.parametrize('outcome', ['failure', 'cancel'])
async def test_device_removal_preserves_failure_and_cancellation(
    device_removal, outcome
):
    ctx = device_removal
    original_data = ctx.entry.data
    timer = ctx.poller._timer
    error = RuntimeError if outcome == 'failure' else asyncio.CancelledError
    ctx.client._storage.save_async.side_effect = error('synthetic save failure')
    with pytest.raises(error):
        await ctx.remove('blt.3.removed')
    assert ctx.entry.data is original_data
    ctx.hass.config_entries.async_update_entry.assert_not_called()
    ctx.registry.async_remove_device.assert_not_called()
    assert ctx.poller.prop_count == 6
    assert ctx.poller._timer is timer
    assert not ctx.client.entry_update_lock.locked()


@pytest.fixture
def removal_options(integration_modules, device_removal):
    """Open real options before removing devices through the real HA path."""
    ctx = device_removal
    ctx.entry.data = {
        **ctx.entry.data, 'virtual_did': 'test-entry', 'uid': 'test-user',
        'storage_path': 'unused', 'cloud_server': 'cn', 'uuid': 'test-uuid',
        'home_selected': {}, 'nick_name': 'Original'}
    ctx.entry.unique_id = 'test-entry'
    ctx.entry.options = {}
    ctx.client._display_devs_notify = []
    ctx.client._persistence_notify = Mock()
    ctx.client._MIoTClient__request_show_devices_changed_notify = Mock()
    for did, info in ctx.client.device_list.items():
        info.update(home_name='Home', room_name='Room', name=did)
    flow = integration_modules.flow.OptionsFlowHandler(ctx.entry)
    flow._main_loop = Mock(spec=asyncio.AbstractEventLoop)
    flow._main_loop.create_task.side_effect = asyncio.create_task
    flow._miot_client = ctx.client
    flow._miot_storage = SimpleNamespace(
        save_async=AsyncMock(return_value=True),
        update_user_config_async=AsyncMock(return_value=True))
    flow._miot_network = SimpleNamespace(
        get_network_status_async=AsyncMock(return_value=True))
    flow.hass = ctx.hass
    flow.hass.config_entries.async_reload = AsyncMock()
    flow.async_show_form = Mock(side_effect=lambda **kwargs: kwargs)
    flow.async_create_entry = Mock(return_value={'type': 'create_entry'})
    flow.async_step_update_lan_ctrl_config = AsyncMock()
    flow._display_binary_mode_new = flow._display_binary_mode
    flow._opt_cloud_poll_cfg = True
    ctx.flow = flow
    return ctx


async def submit_poll_options(flow, selection, interval=120):
    """Submit through the production schema, including HA's multi-select."""
    form = await flow.async_step_cloud_poll_config()
    data = {'cloud_poll_interval': interval}
    if selection is not None:
        data['cloud_poll_device_ids'] = selection
    await flow.async_step_cloud_poll_config(form['data_schema'](data))


@pytest.mark.parametrize('last', [False, True])
async def test_stale_options_untouched_after_device_removal(
    removal_options, last
):
    ctx = removal_options
    flow = ctx.flow
    flow._opt_cloud_poll_cfg = False
    await flow.async_step_cloud_poll_config()
    assert await ctx.remove('blt.3.removed')
    if last:
        assert await ctx.remove('z.kept')
    # Cache is not authoritative for removal.
    assert 'blt.3.removed' in ctx.client.device_list
    ctx.entry.data = {
        **ctx.entry.data, 'nick_name': 'Concurrent',
        'display_devices_changed_notify': [], 'new_field': {'keep': True}}
    del ctx.entry.data['unrelated']
    latest = ctx.entry.data.copy()

    assert await flow.async_step_config_confirm({'confirm': True}) == {
        'type': 'create_entry'}

    assert ctx.entry.data == latest
    assert ctx.entry.title.startswith('Concurrent: ')
    assert ctx.client.display_devices_changed_notify == []
    flow._main_loop.call_later.assert_not_called()
    assert ctx.poller.active is not last


@pytest.mark.parametrize('last', [False, True])
async def test_stale_options_selection_conflict_requires_restart(
    removal_options, last
):
    ctx = removal_options
    flow = ctx.flow
    await submit_poll_options(flow, ['blt.3.removed', 'unselected'])
    assert await ctx.remove('blt.3.removed')
    if last:
        assert await ctx.remove('z.kept')
    latest = ctx.entry.data.copy()
    ctx.hass.config_entries.async_update_entry.reset_mock()

    with pytest.raises(ImportOnlyAbortFlow) as caught:
        await flow.async_step_config_confirm({'confirm': True})

    assert caught.value.reason == 'options_flow_error'
    assert 'reopen' in caught.value.description_placeholders['error'].lower()
    assert ctx.entry.data == latest
    ctx.hass.config_entries.async_update_entry.assert_not_called()
    flow._miot_storage.save_async.assert_not_awaited()
    flow._main_loop.call_later.assert_not_called()


@pytest.mark.parametrize('mode', [
    'addition', 'clear', 'omitted', 'unchanged', 'interval',
    'disjoint_selection', 'disjoint_interval',
    'converged_selection', 'converged_interval'])
async def test_stale_options_merges_changes_and_reload_decisions(
    removal_options, mode
):
    ctx = removal_options
    flow = ctx.flow
    selection = ['blt.3.removed', 'z.kept']
    interval = 120
    if mode in ['addition', 'disjoint_selection']:
        selection.append('unselected')
    elif mode in ['clear', 'converged_selection']:
        selection = []
    elif mode == 'omitted':
        selection = None
    if mode in ['interval', 'disjoint_interval', 'converged_interval']:
        interval = 240
    await submit_poll_options(flow, selection, interval)
    ctx.entry.data = {**ctx.entry.data, 'unrelated': {'current': True}}
    if mode == 'disjoint_selection':
        ctx.entry.data['cloud_poll_interval'] = 180
    if mode in ['disjoint_interval', 'converged_selection']:
        assert await ctx.remove('blt.3.removed')
        assert await ctx.remove('z.kept')
    if mode == 'converged_interval':
        ctx.entry.data['cloud_poll_interval'] = interval

    await flow.async_step_config_confirm({'confirm': True})

    assert ctx.entry.data['cloud_poll_device_ids'] == (
        [] if mode == 'disjoint_interval' else selection or [])
    assert ctx.entry.data['cloud_poll_interval'] == (
        180 if mode == 'disjoint_selection' else interval)
    assert ctx.entry.data['unrelated'] == {'current': True}
    if mode in ['unchanged', 'converged_selection', 'converged_interval']:
        flow._main_loop.call_later.assert_not_called()
    else:
        flow._main_loop.call_later.assert_called_once()
        delay, callback = flow._main_loop.call_later.call_args.args
        assert delay == 0
        await callback()
        flow.hass.config_entries.async_reload.assert_awaited_once_with(
            entry_id='entry')


async def test_stale_options_interval_conflict_requires_restart(
    removal_options
):
    ctx = removal_options
    await submit_poll_options(
        ctx.flow, ['blt.3.removed', 'z.kept'], interval=240)
    ctx.entry.data = {**ctx.entry.data, 'cloud_poll_interval': 180}
    with pytest.raises(ImportOnlyAbortFlow) as caught:
        await ctx.flow.async_step_config_confirm({'confirm': True})
    assert caught.value.reason == 'options_flow_error'
    assert ctx.entry.data['cloud_poll_interval'] == 180
    ctx.hass.config_entries.async_update_entry.assert_not_called()
    ctx.flow._main_loop.call_later.assert_not_called()


@pytest.mark.parametrize('edit_interval', [False, True])
@pytest.mark.parametrize('keep_added_device', [False, True])
async def test_effective_poll_selection_respects_proposed_imports(
    coordinated_options, edit_interval, keep_added_device
):
    ctx = coordinated_options
    flow = ctx.flow
    ctx.release.set()
    flow._update_devices = True
    flow._device_list_sorted = {
        did: info for did, info in ctx.client.device_list.items()
        if keep_added_device or did != 'unselected'}
    flow._devices_remove = [] if keep_added_device else ['unselected']
    if edit_interval:
        await submit_poll_options(
            flow, ['blt.3.removed', 'z.kept'], interval=240)
    else:
        flow._opt_cloud_poll_cfg = False
        await flow.async_step_cloud_poll_config()
    # Another flow completed this selection edit after the old form opened.
    ctx.entry.data = {
        **ctx.entry.data,
        'cloud_poll_device_ids': ['blt.3.removed', 'z.kept', 'unselected'],
        'unrelated': {'concurrent': True}}
    latest = deepcopy(ctx.entry.data)
    persisted = deepcopy(ctx.persisted)
    timer = ctx.poller._timer

    if not keep_added_device:
        with pytest.raises(ImportOnlyAbortFlow) as caught:
            await flow.async_step_config_confirm({'confirm': True})
        assert caught.value.reason == 'options_flow_error'
        assert 'reopen' in (
            caught.value.description_placeholders['error'].lower())
        assert ctx.entry.data == latest
        assert ctx.persisted == persisted
        assert not ctx.writes
        flow._miot_storage.save_async.assert_not_awaited()
        flow._miot_storage.update_user_config_async.assert_not_awaited()
        ctx.hass.config_entries.async_update_entry.assert_not_called()
        flow.async_create_entry.assert_not_called()
        flow._main_loop.call_later.assert_not_called()
        notify = ctx.client._MIoTClient__request_show_devices_changed_notify
        notify.assert_not_called()
    else:
        assert await flow.async_step_config_confirm({'confirm': True}) == {
            'type': 'create_entry'}
        assert ctx.entry.data == {
            **latest, 'cloud_poll_interval': 240 if edit_interval else 120}
        assert ctx.persisted['devices'] == flow._device_list_sorted
        assert set(ctx.entry.data['cloud_poll_device_ids']) <= set(
            ctx.persisted['devices'])
        assert ctx.writes == ['flow']
        flow._miot_storage.update_user_config_async.assert_not_awaited()
        flow._main_loop.call_later.assert_called_once()
    ctx.registry.async_remove_device.assert_not_called()
    assert ctx.poller.prop_count == 6
    assert ctx.poller._timer is timer
    assert not ctx.client.entry_update_lock.locked()


@pytest.mark.parametrize('last', [False, True])
async def test_effective_poll_selection_allows_deliberate_pruning(
    coordinated_options, last
):
    ctx = coordinated_options
    flow = ctx.flow
    ctx.release.set()
    flow._opt_cloud_poll_cfg = False
    flow._update_devices = True
    flow._devices_remove = (
        ['blt.3.removed', 'z.kept'] if last else ['blt.3.removed'])
    flow._device_list_sorted = {
        did: info for did, info in ctx.client.device_list.items()
        if did not in flow._devices_remove}
    await flow.async_step_cloud_poll_config()
    # A disjoint interval edit must not prevent this flow's deliberate pruning.
    ctx.entry.data = {**ctx.entry.data, 'cloud_poll_interval': 180}
    latest = deepcopy(ctx.entry.data)
    assert await flow.async_step_config_confirm({'confirm': True}) == {
        'type': 'create_entry'}
    assert ctx.entry.data == {
        **latest, 'cloud_poll_device_ids': [] if last else ['z.kept']}
    assert ctx.persisted == {
        'devices': flow._device_list_sorted,
        'devices_remove': flow._devices_remove}
    assert ctx.writes == ['flow', 'devices_remove']
    flow._main_loop.call_later.assert_called_once()
    ctx.registry.async_remove_device.assert_not_called()
    assert not ctx.client.entry_update_lock.locked()


@pytest.fixture
def coordinated_options(removal_options):
    """Two storage interfaces backed by the same synthetic persisted state."""
    ctx = removal_options
    ctx.persisted = {'devices': deepcopy(ctx.client.device_list)}
    ctx.writes = []
    ctx.started = asyncio.Event()
    ctx.release = asyncio.Event()
    ctx.block_writer = 'flow'

    async def save(writer, *, data, **_kwargs):
        if writer == ctx.block_writer:
            ctx.started.set()
            await ctx.release.wait()
        ctx.persisted['devices'] = deepcopy(data)
        ctx.writes.append(writer)
        return True

    async def save_flow(*, data, **kwargs):
        return await save('flow', data=data, **kwargs)

    async def save_client(*, data, **kwargs):
        return await save('client', data=data, **kwargs)

    async def save_removals(**kwargs):
        ctx.persisted.update(deepcopy(kwargs['config']))
        ctx.writes.append('devices_remove')
        return True

    ctx.flow._miot_storage.save_async.side_effect = save_flow
    ctx.client._storage.save_async.side_effect = save_client
    ctx.client._storage.load_async.side_effect = lambda **_: deepcopy(
        ctx.persisted['devices'])
    ctx.flow._miot_storage.update_user_config_async.side_effect = save_removals
    return ctx


async def start_concurrent_operation(coro):
    """Let an operation reach its first suspension before inspecting it."""
    started = asyncio.Event()

    async def run():
        started.set()
        return await coro

    task = asyncio.create_task(run())
    await started.wait()
    return task


@pytest.mark.parametrize('did', ['blt.3.removed', 'unselected', 'unknown'])
async def test_entry_writes_confirmation_before_removal(
    coordinated_options, did
):
    ctx = coordinated_options
    flow = ctx.flow
    await submit_poll_options(flow, ['blt.3.removed', 'unselected'])
    flow._update_devices = True
    flow._device_list_sorted = {
        did: info for did, info in ctx.client.device_list.items()
        if did != 'z.kept'}
    flow._devices_remove = ['z.kept']
    confirm = asyncio.create_task(
        flow.async_step_config_confirm({'confirm': True}))
    tasks = [confirm]
    try:
        await asyncio.wait_for(ctx.started.wait(), timeout=5)
        removal = await start_concurrent_operation(ctx.remove(did))
        tasks.append(removal)
        assert not removal.done()
        ctx.client._storage.save_async.assert_not_awaited()
        ctx.registry.async_remove_device.assert_not_called()
        # Unrelated current data must still survive the awaited save.
        ctx.entry.data = {**ctx.entry.data, 'unrelated': {'during_save': True}}
        ctx.release.set()
        assert await asyncio.wait_for(confirm, timeout=5) == {
            'type': 'create_entry'}
        assert await asyncio.wait_for(removal, timeout=5)
        assert ctx.writes == (
            ['flow', 'devices_remove']
            + ([] if did == 'unknown' else ['client']))
        assert ctx.persisted['devices'] == flow._device_list_sorted
        assert ctx.persisted['devices_remove'] == ['z.kept']
        assert ctx.entry.data['cloud_poll_device_ids'] == [
            selected for selected in ['blt.3.removed', 'unselected']
            if selected != did]
        assert set(ctx.entry.data['cloud_poll_device_ids']) <= set(
            ctx.persisted['devices'])
        assert ctx.entry.data['unrelated'] == {'during_save': True}
        flow._main_loop.call_later.assert_called_once()
    finally:
        ctx.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize('last', [False, True])
async def test_entry_writes_removal_before_conflicting_confirmation(
    coordinated_options, last
):
    ctx = coordinated_options
    flow = ctx.flow
    await submit_poll_options(flow, ['blt.3.removed', 'unselected'])
    flow._update_devices = True
    flow._device_list_sorted = {
        did: info for did, info in ctx.client.device_list.items()
        if did != 'z.kept'}
    flow._devices_remove = ['z.kept']
    ctx.block_writer = 'client'
    removal = asyncio.create_task(ctx.remove('blt.3.removed'))
    tasks = [removal]
    try:
        await asyncio.wait_for(ctx.started.wait(), timeout=5)
        if last:
            tasks.append(await start_concurrent_operation(ctx.remove('z.kept')))
        confirm = await start_concurrent_operation(
            flow.async_step_config_confirm({'confirm': True}))
        tasks.append(confirm)
        flow._miot_storage.save_async.assert_not_awaited()
        ctx.release.set()
        assert await asyncio.wait_for(removal, timeout=5)
        if last:
            assert await asyncio.wait_for(tasks[1], timeout=5)
        with pytest.raises(ImportOnlyAbortFlow) as caught:
            await asyncio.wait_for(confirm, timeout=5)
        assert caught.value.reason == 'options_flow_error'
        assert ctx.writes == ['client'] * (2 if last else 1)
        assert 'z.kept' in ctx.persisted['devices']
        assert 'devices_remove' not in ctx.persisted
        assert ctx.entry.data['cloud_poll_device_ids'] == (
            [] if last else ['z.kept'])
        flow._miot_storage.save_async.assert_not_awaited()
        flow._miot_storage.update_user_config_async.assert_not_awaited()
        flow._main_loop.call_later.assert_not_called()
    finally:
        ctx.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize('holder', ['flow', 'client'])
@pytest.mark.parametrize('cancel_waiter', [False, True])
async def test_entry_writes_cancellation_releases_only_owned_lock(
    coordinated_options, holder, cancel_waiter
):
    ctx = coordinated_options
    flow = ctx.flow
    await submit_poll_options(flow, ['blt.3.removed', 'z.kept'])
    flow._update_devices = True
    flow._device_list_sorted = ctx.client.device_list.copy()
    ctx.block_writer = holder

    def confirmation():
        return flow.async_step_config_confirm({'confirm': True})

    def removal():
        return ctx.remove('blt.3.removed')

    first, second = (confirmation, removal) if holder == 'flow' else (
        removal, confirmation)
    task = asyncio.create_task(first())
    tasks = [task]
    try:
        await asyncio.wait_for(ctx.started.wait(), timeout=5)
        waiter = await start_concurrent_operation(second())
        tasks.append(waiter)
        assert not waiter.done()
        cancelled = waiter if cancel_waiter else task
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        if cancel_waiter:
            assert not task.done()
            assert not ctx.writes
            ctx.release.set()
            await asyncio.wait_for(task, timeout=5)
            assert ctx.writes == [holder]
        else:
            await asyncio.wait_for(waiter, timeout=5)
            assert ctx.writes == [('client' if holder == 'flow' else 'flow')]
        # A subsequent operation can acquire the lock after either cancellation.
        ctx.release.set()
        assert await asyncio.wait_for(ctx.remove('unknown'), timeout=5)
    finally:
        ctx.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize('failure', ['false', 'exception'])
async def test_entry_writes_failed_confirmation_releases_lock(
    coordinated_options, failure
):
    ctx = coordinated_options
    flow = ctx.flow
    await submit_poll_options(flow, ['unselected'])
    flow._update_devices = True
    flow._device_list_sorted = {
        'unselected': ctx.client.device_list['unselected']}
    saved = deepcopy(ctx.persisted)
    current = deepcopy(ctx.entry.data)
    if failure == 'false':
        flow._miot_storage.save_async.side_effect = None
        flow._miot_storage.save_async.return_value = False
        error = ImportOnlyAbortFlow
    else:
        flow._miot_storage.save_async.side_effect = OSError('synthetic failure')
        error = OSError
    with pytest.raises(error):
        await flow.async_step_config_confirm({'confirm': True})
    assert ctx.persisted == saved
    assert ctx.entry.data == current
    flow._main_loop.call_later.assert_not_called()
    assert not ctx.client.entry_update_lock.locked()
    assert await asyncio.wait_for(ctx.remove('blt.3.removed'), timeout=5)


@pytest.mark.parametrize('source', ['cloud', 'lan', 'gateway'])
@pytest.mark.parametrize('failure', [
    'load_invalid', 'load_exception', 'save_false', 'save_exception',
    'load_cancel', 'save_cancel'])
async def test_removal_storage_failure_preserves_subscriptions_and_retry(
    integration_modules, device_removal, source, failure
):
    ctx = device_removal
    did = 'blt.3.removed'
    transport = SimpleNamespace(unsub_prop=Mock(), unsub_event=Mock())
    if source == 'cloud':
        ctx.client._mips_cloud = transport
    elif source == 'lan':
        ctx.client._miot_lan = transport
    else:
        ctx.client._mips_local[source] = transport
    ctx.client._sub_source_list[did] = source
    sources = ctx.client._sub_source_list.copy()
    notify = Mock()
    ctx.client._MIoTClient__request_show_devices_changed_notify = notify
    current = deepcopy(ctx.entry.data)
    timer = ctx.poller._timer
    load = ctx.client._storage.load_async
    save = ctx.client._storage.save_async
    failing_io = load if failure.startswith('load') else save
    failing_io.side_effect = None

    if failure.endswith('cancel'):
        started = asyncio.Event()

        async def wait_for_io(**_kwargs):
            started.set()
            await asyncio.Event().wait()

        failing_io.side_effect = wait_for_io
        task = asyncio.create_task(ctx.remove(did))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    else:
        error = integration_modules.client.MIoTClientError
        if failure.endswith('exception'):
            error = OSError
            failing_io.side_effect = OSError('synthetic storage failure')
        else:
            failing_io.return_value = (
                None if failure == 'load_invalid' else False)
        with pytest.raises(error):
            await ctx.remove(did)

    assert ctx.client._sub_source_list == sources
    transport.unsub_prop.assert_not_called()
    transport.unsub_event.assert_not_called()
    notify.assert_not_called()
    assert ctx.entry.data == current
    assert ctx.poller.prop_count == 6
    assert ctx.poller.active
    assert ctx.poller._timer is timer
    ctx.hass.config_entries.async_update_entry.assert_not_called()
    ctx.registry.async_remove_device.assert_not_called()
    assert not ctx.client.entry_update_lock.locked()
    if failure.startswith('load'):
        save.assert_not_awaited()

    # A successful retry tears down the retained route exactly once.
    load.side_effect = None
    load.return_value = deepcopy(ctx.client.device_list)
    save.side_effect = None
    save.return_value = True
    assert await asyncio.wait_for(ctx.remove(did), timeout=5)
    assert ctx.client._sub_source_list == {
        key: value for key, value in sources.items() if key != did}
    transport.unsub_prop.assert_called_once_with(did=did)
    transport.unsub_event.assert_called_once_with(did=did)
    notify.assert_called_once_with()
    assert ctx.entry.data == {**current, 'cloud_poll_device_ids': ['z.kept']}
    assert ctx.poller.prop_count == 3
    assert ctx.poller.active
    assert ctx.poller._timer is timer
    ctx.registry.async_remove_device.assert_called_once_with('registry-id')
    ctx.hass.config_entries.async_update_entry.assert_called_once()
    assert not ctx.client.entry_update_lock.locked()


async def test_entry_writes_do_not_block_another_entry(
    integration_modules, coordinated_options
):
    ctx = coordinated_options
    other = cloud_client.__wrapped__(integration_modules)
    other._uid = 'other-user'
    other._display_devs_notify = []
    other._storage = SimpleNamespace(
        load_async=AsyncMock(return_value={'selected': {}}),
        save_async=AsyncMock(return_value=True))
    entry = SimpleNamespace(entry_id='other-entry', data={})
    integration = integration_modules.integration
    ctx.hass.data[integration.DOMAIN]['miot_clients'][entry.entry_id] = other
    assert ctx.client.entry_update_lock is not other.entry_update_lock
    async with ctx.client.entry_update_lock:
        assert await asyncio.wait_for(
            integration.async_remove_config_entry_device(
                ctx.hass, entry, SimpleNamespace(
                    id='other-device', identifiers={(
                        integration.DOMAIN,
                        integration.slugify_did('cn', 'selected'))})),
            timeout=5)
        assert ctx.client.entry_update_lock.locked()
    other._storage.save_async.assert_awaited_once()
    assert not other.entry_update_lock.locked()
    assert not ctx.writes


@pytest.mark.parametrize('unload_first', [False, True])
async def test_entry_writes_coordinate_unload_and_retire_waiters(
    integration_modules, coordinated_options, unload_first
):
    ctx = coordinated_options
    flow = ctx.flow
    integration = integration_modules.integration
    domain_data = ctx.hass.data[integration.DOMAIN]
    domain_data.update(devices={'entry': ctx.devices}, entities={'entry': []})
    ctx.client.deinit_async = AsyncMock()
    unload_started = asyncio.Event()
    release_unload = asyncio.Event()

    async def unload(*_args):
        unload_started.set()
        if unload_first:
            await release_unload.wait()
        return True

    ctx.hass.config_entries.async_unload_platforms = AsyncMock(
        side_effect=unload)
    await submit_poll_options(flow, ['blt.3.removed', 'unselected'])
    flow._update_devices = True
    flow._device_list_sorted = ctx.client.device_list.copy()
    tasks = []
    try:
        if unload_first:
            unloading = asyncio.create_task(
                integration.async_unload_entry(ctx.hass, ctx.entry))
            tasks.append(unloading)
            await asyncio.wait_for(unload_started.wait(), timeout=5)
            confirm = await start_concurrent_operation(
                flow.async_step_config_confirm({'confirm': True}))
            tasks.append(confirm)
            flow._miot_storage.save_async.assert_not_awaited()
        else:
            confirm = asyncio.create_task(
                flow.async_step_config_confirm({'confirm': True}))
            tasks.append(confirm)
            await asyncio.wait_for(ctx.started.wait(), timeout=5)
            unloading = await start_concurrent_operation(
                integration.async_unload_entry(ctx.hass, ctx.entry))
            tasks.append(unloading)
            ctx.hass.config_entries.async_unload_platforms.assert_not_awaited()
        removal = await start_concurrent_operation(ctx.remove('blt.3.removed'))
        tasks.append(removal)
        assert not removal.done()
        ctx.release.set()
        release_unload.set()
        assert await asyncio.wait_for(unloading, timeout=5)
        if unload_first:
            with pytest.raises(ImportOnlyAbortFlow) as caught:
                await asyncio.wait_for(confirm, timeout=5)
            assert 'reopen' in caught.value.description_placeholders['error']
            assert not ctx.writes
            flow._main_loop.call_later.assert_not_called()
        else:
            assert await asyncio.wait_for(confirm, timeout=5) == {
                'type': 'create_entry'}
            assert ctx.writes == ['flow']
            flow._main_loop.call_later.assert_called_once()
        assert not await asyncio.wait_for(removal, timeout=5)
        assert 'entry' not in domain_data['miot_clients']
        assert not ctx.client.entry_update_lock.locked()
        assert not ctx.poller.active
        ctx.client.deinit_async.assert_awaited_once()
        ctx.registry.async_remove_device.assert_not_called()
        # Even after a replacement client exists, the old flow cannot commit.
        replacement = cloud_client.__wrapped__(integration_modules)
        domain_data['miot_clients']['entry'] = replacement
        with pytest.raises(ImportOnlyAbortFlow):
            await flow.async_step_config_confirm({'confirm': True})
        assert not replacement.entry_update_lock.locked()
    finally:
        ctx.release.set()
        release_unload.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_entry_writes_do_not_hold_lock_on_forms(removal_options):
    ctx = removal_options
    async with ctx.client.entry_update_lock:
        form = await asyncio.wait_for(
            ctx.flow.async_step_cloud_poll_config(), timeout=5)
        assert form['step_id'] == 'cloud_poll_config'
        ctx.flow._miot_storage.save_async.assert_not_awaited()


async def test_stale_options_back_navigation_discards_disabled_poll_edit(
    removal_options
):
    ctx = removal_options
    flow = ctx.flow
    await submit_poll_options(flow, ['unselected'], interval=240)
    # Resubmit the preceding page with polling configuration unchecked.
    await flow.async_step_config_options({'update_cloud_poll_config': False})
    assert await ctx.remove('blt.3.removed')
    latest = ctx.entry.data.copy()
    await flow.async_step_config_confirm({'confirm': True})
    assert ctx.entry.data == latest
    flow._main_loop.call_later.assert_not_called()


async def test_stale_options_merges_other_edits_preserving_current_fields(
    removal_options
):
    ctx = removal_options
    flow = ctx.flow
    await flow.async_step_config_options({
        'update_cloud_poll_config': False, 'action_debug': True,
        'display_devices_changed_notify': ['offline']})
    assert await ctx.remove('blt.3.removed')
    ctx.entry.data = {
        **ctx.entry.data, 'ctrl_mode': 'cloud', 'cover_dead_zone_width': 7,
        'display_binary_mode': ['bool'], 'hide_non_standard_entities': True}
    latest = ctx.entry.data.copy()
    await flow.async_step_config_confirm({'confirm': True})
    assert ctx.entry.data == {
        **latest, 'action_debug': True,
        'display_devices_changed_notify': ['offline']}
    assert ctx.client.display_devices_changed_notify == ['offline']
    flow._main_loop.call_later.assert_called_once()


@pytest.mark.parametrize('last', [False, True])
@pytest.mark.parametrize('outcome', ['false', 'error', 'cancel'])
async def test_unload_after_inflight_removal_preserves_lifecycle(
    integration_modules, device_removal, last, outcome
):
    ctx = device_removal
    integration = integration_modules.integration
    started = asyncio.Event()
    cancelled = asyncio.Event()
    post = ctx.client._http._MIoTHttpClient__mihome_api_post_async

    async def response(**_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    post.side_effect = response
    ctx.client.deinit_async = AsyncMock()
    unload = AsyncMock(return_value=False)
    error = RuntimeError if outcome == 'error' else asyncio.CancelledError
    if outcome != 'false':
        unload.side_effect = error('synthetic unload failure')
    ctx.hass.config_entries.async_unload_platforms = unload
    ctx.poller._timer.cancel()
    ctx.poller._MIoTCloudPoller__start_poll()
    task = ctx.poller._task
    await asyncio.wait_for(started.wait(), timeout=5)
    assert await ctx.remove('blt.3.removed')
    if last:
        assert await ctx.remove('z.kept')

    if outcome == 'false':
        assert not await integration.async_unload_entry(ctx.hass, ctx.entry)
    else:
        with pytest.raises(error):
            await integration.async_unload_entry(ctx.hass, ctx.entry)

    assert cancelled.is_set()
    assert task.cancelled()
    assert ctx.poller.active == (not last and outcome != 'cancel')
    assert (ctx.poller._timer is not None) == ctx.poller.active
    assert not ctx.poller._poll_lock.locked()
    ctx.client.deinit_async.assert_not_awaited()
    assert ctx.hass.data[integration.DOMAIN]['cloud_pollers']['entry'] is (
        ctx.poller)
    post.assert_awaited_once()
    post.side_effect = lambda **kwargs: {'result': [
        {**param, 'value': 0} for param in kwargs['data']['params']]}
    assert await ctx.poller.async_poll_once() == (0 if last else 3)
    assert all(param['did'] == 'z.kept'
               for request in post.await_args_list[1:]
               for param in request.kwargs['data']['params'])
