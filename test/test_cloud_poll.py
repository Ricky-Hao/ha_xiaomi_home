# -*- coding: utf-8 -*-
"""Tests for selected-device cloud property polling."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

# pylint: disable=import-outside-toplevel, protected-access
# pylint: disable=redefined-outer-name

pytestmark = pytest.mark.github


def cloud_prop_key(param: dict) -> str:
    """Build the property key returned by the client API."""
    did = param['did']
    siid = param['siid']
    piid = param['piid']
    return f'{did}|{siid}|{piid}'


class FakeProp:
    """Minimal readable MIoT property."""

    def __init__(self, siid: int, piid: int, readable: bool = True) -> None:
        self.service = SimpleNamespace(iid=siid)
        self.iid = piid
        self.readable = readable


class FakeDevice:
    """Minimal transformed MIoT device."""

    def __init__(
        self, did: str, props: list[FakeProp], entity_props: list[FakeProp]
    ) -> None:
        self.did = did
        self.prop_list = {'sensor': props}
        self.entity_list = {
            'climate': [SimpleNamespace(props=set(entity_props))]}


class FakeClient:
    """Cloud refresh client recording requested batches."""

    def __init__(self) -> None:
        self.main_loop = asyncio.get_running_loop()
        self.calls: list[list[dict]] = []

    async def refresh_cloud_props_async(
        self, params: list[dict]
    ) -> set[str]:
        self.calls.append(params)
        return {cloud_prop_key(param) for param in params}


@pytest.mark.asyncio
async def test_cloud_poll_selects_readable_entity_properties():
    from miot.miot_cloud_poll import MIoTCloudPoller

    prop_shared = FakeProp(siid=2, piid=1)
    prop_entity = FakeProp(siid=3, piid=1)
    prop_unreadable = FakeProp(siid=2, piid=2, readable=False)
    client = FakeClient()
    poller = MIoTCloudPoller(
        miot_client=client,
        devices=[
            FakeDevice(
                did='selected',
                props=[prop_shared, prop_unreadable],
                entity_props=[prop_shared, prop_entity]),
            FakeDevice(
                did='ignored',
                props=[FakeProp(siid=2, piid=1)],
                entity_props=[]),
        ],
        selected_dids={'selected'},
    )

    assert poller.prop_count == 2
    assert await poller.async_poll_once() == 2
    assert client.calls == [[
        {'did': 'selected', 'siid': 2, 'piid': 1},
        {'did': 'selected', 'siid': 3, 'piid': 1},
    ]]


@pytest.mark.asyncio
async def test_cloud_poll_batches_requests():
    from miot.miot_cloud_poll import MIoTCloudPoller

    client = FakeClient()
    poller = MIoTCloudPoller(
        miot_client=client,
        devices=[
            FakeDevice(
                did='selected',
                props=[FakeProp(siid=2, piid=piid) for piid in range(1, 306)],
                entity_props=[]),
        ],
        selected_dids={'selected'},
    )

    assert await poller.async_poll_once() == 305
    assert [len(batch) for batch in client.calls] == [150, 150, 5]


@pytest.mark.asyncio
async def test_cloud_poll_skips_overlapping_round():
    from miot.miot_cloud_poll import MIoTCloudPoller

    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingClient(FakeClient):
        async def refresh_cloud_props_async(
            self, params: list[dict]
        ) -> set[str]:
            self.calls.append(params)
            started.set()
            await release.wait()
            return {'selected|2|1'}

    client = BlockingClient()
    poller = MIoTCloudPoller(
        miot_client=client,
        devices=[
            FakeDevice(
                did='selected',
                props=[FakeProp(siid=2, piid=1)],
                entity_props=[]),
        ],
        selected_dids={'selected'},
    )

    first_round = asyncio.create_task(poller.async_poll_once())
    await started.wait()
    assert await poller.async_poll_once() == 0
    release.set()
    assert await first_round == 1
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_cloud_poll_start_and_stop():
    from miot.miot_cloud_poll import MIoTCloudPoller

    client = FakeClient()
    poller = MIoTCloudPoller(
        miot_client=client,
        devices=[
            FakeDevice(
                did='selected',
                props=[FakeProp(siid=2, piid=1)],
                entity_props=[]),
        ],
        selected_dids={'selected'},
    )

    assert poller.start()
    assert poller.active
    await poller.stop()
    assert not poller.active


@pytest.mark.asyncio
async def test_cloud_poll_interval_bounds():
    from miot.miot_cloud_poll import MIoTCloudPoller

    client = FakeClient()
    devices = [
        FakeDevice(
            did='selected',
            props=[FakeProp(siid=2, piid=1)],
            entity_props=[]),
    ]

    for interval in [30, 3600]:
        poller = MIoTCloudPoller(
            miot_client=client,
            devices=devices,
            selected_dids={'selected'},
            interval=interval,
        )
        assert poller.prop_count == 1
    for interval in [29, 3601]:
        with pytest.raises(ValueError):
            MIoTCloudPoller(
                miot_client=client,
                devices=devices,
                selected_dids={'selected'},
                interval=interval,
            )


@pytest.mark.asyncio
async def test_cloud_poll_does_not_start_without_selected_properties():
    from miot.miot_cloud_poll import MIoTCloudPoller

    poller = MIoTCloudPoller(
        miot_client=FakeClient(),
        devices=[
            FakeDevice(
                did='ignored',
                props=[FakeProp(siid=2, piid=1)],
                entity_props=[]),
        ],
        selected_dids=set(),
    )

    assert not poller.start()
    assert not poller.active


@pytest.fixture
def scheduled_poller():
    """Use real tasks and manually fire timers without wall-clock waits."""
    from miot.miot_cloud_poll import MIoTCloudPoller

    loop = Mock(spec=asyncio.AbstractEventLoop)
    loop.create_task.side_effect = asyncio.create_task
    loop.call_later.side_effect = lambda *_: Mock(spec=asyncio.TimerHandle)
    client = SimpleNamespace(
        main_loop=loop, refresh_cloud_props_async=AsyncMock())
    poller = MIoTCloudPoller(
        miot_client=client,
        devices=[FakeDevice(
            'selected', [FakeProp(2, piid) for piid in range(1, 4)], [])],
        selected_dids={'selected'},
        batch_size=1)
    return poller, client, loop


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['miot', 'timeout', 'unexpected'])
async def test_batch_failure_continues_and_next_round_recovers(
    scheduled_poller, failure
):
    from miot.miot_error import MIoTClientError

    poller, client, _ = scheduled_poller
    error = {
        'miot': MIoTClientError('failed batch'),
        'timeout': TimeoutError('request timed out'),
        'unexpected': RuntimeError('unexpected request failure'),
    }[failure]
    client.refresh_cloud_props_async.side_effect = [
        error, {'selected|2|2'}, {'selected|2|3'},
        {'selected|2|1'}, {'selected|2|2'}, {'selected|2|3'}]

    assert await poller.async_poll_once() == 2
    assert await poller.async_poll_once() == 3
    assert [
        call.args[0][0]['piid']
        for call in client.refresh_cloud_props_async.await_args_list
    ] == [1, 2, 3, 1, 2, 3]


@pytest.mark.asyncio
@pytest.mark.parametrize('fail', [False, True])
async def test_timer_waits_for_round_completion(scheduled_poller, fail):
    poller, client, loop = scheduled_poller
    started = asyncio.Event()
    release = asyncio.Event()

    async def refresh(params):
        started.set()
        await release.wait()
        if fail:
            raise TimeoutError('request timed out')
        return {cloud_prop_key(param) for param in params}

    client.refresh_cloud_props_async.side_effect = refresh
    assert poller.start()
    assert poller.start()
    assert loop.call_later.call_count == 1
    assert loop.call_later.call_args.args[0] == 60
    client.refresh_cloud_props_async.assert_not_awaited()

    loop.call_later.call_args.args[1]()
    task = poller._task
    await started.wait()
    assert loop.call_later.call_count == 1
    release.set()
    await task

    assert client.refresh_cloud_props_async.await_count == 3
    assert loop.call_later.call_count == 2
    assert loop.call_later.call_args.args[0] == 60
    timer = poller._timer
    await poller.stop()
    timer.cancel.assert_called_once()
    assert loop.call_later.call_count == 2


@pytest.mark.asyncio
async def test_stop_cancels_active_request_without_rescheduling(
    scheduled_poller
):
    poller, client, loop = scheduled_poller
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def refresh(params):
        assert params == [{'did': 'selected', 'siid': 2, 'piid': 1}]
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    client.refresh_cloud_props_async.side_effect = refresh
    poller.start()
    callback = loop.call_later.call_args.args[1]
    callback()
    task = poller._task
    await started.wait()
    await poller.stop()
    await poller.stop()

    assert cancelled.is_set()
    assert task.cancelled()
    assert not poller.active
    assert not poller._poll_lock.locked()
    assert poller._task is None
    assert poller._timer is None
    callback()  # A stale timer callback must not restart a stopped poller.
    assert loop.call_later.call_count == 1
    assert client.refresh_cloud_props_async.await_count == 1


@pytest.mark.asyncio
async def test_selected_device_without_readable_properties():
    from miot.miot_cloud_poll import MIoTCloudPoller

    client = FakeClient()
    poller = MIoTCloudPoller(
        miot_client=client,
        devices=[FakeDevice('selected', [FakeProp(2, 1, False)], [])],
        selected_dids={'selected'})
    assert not poller.start()
    assert await poller.async_poll_once() == 0
    assert not client.calls


@pytest.mark.asyncio
async def test_remove_device_prunes_rounds_and_stops_last_timer():
    from miot.miot_cloud_poll import MIoTCloudPoller

    client = FakeClient()
    poller = MIoTCloudPoller(
        client, [FakeDevice(did, [FakeProp(2, 1)], [])
                 for did in ['removed', 'remaining', 'unselected']],
        {'removed', 'remaining'}, interval=120)
    assert poller.start()
    timer = poller._timer
    try:
        poller.remove_device('unknown')
        poller.remove_device('unselected')
        assert poller.prop_count == 2
        assert poller._timer is timer
        poller.remove_device('removed')
        assert poller.active
        assert poller.prop_count == 1
        assert poller._timer is timer
        for _ in range(2):
            assert await poller.async_poll_once() == 1
        assert client.calls == [
            [{'did': 'remaining', 'siid': 2, 'piid': 1}]] * 2

        poller.remove_device('remaining')
        poller.remove_device('remaining')
        assert not poller.active
        assert timer.cancelled()
        assert poller._timer is None
        assert poller.prop_count == 0
        poller._MIoTCloudPoller__start_poll()
        assert poller._task is None
        assert not poller.start()
        assert await poller.async_poll_once() == 0
        assert len(client.calls) == 2
    finally:
        await poller.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('last', [False, True])
@pytest.mark.parametrize('outcome', ['success', 'failure', 'stop'])
async def test_remove_device_during_batch(last, outcome):
    from miot.miot_cloud_poll import MIoTCloudPoller

    loop = Mock(spec=asyncio.AbstractEventLoop)
    loop.create_task.side_effect = asyncio.create_task
    loop.call_later.side_effect = lambda *_: Mock(spec=asyncio.TimerHandle)
    started = asyncio.Event()
    release = asyncio.Event()
    client = FakeClient()
    client.main_loop = loop

    async def refresh(params):
        client.calls.append(params)
        if not started.is_set():
            started.set()
            await release.wait()
            if outcome == 'failure':
                raise TimeoutError('synthetic request failure')
        return {cloud_prop_key(param) for param in params}

    client.refresh_cloud_props_async = refresh
    devices = [FakeDevice(
        'a.removed', [FakeProp(2, piid) for piid in range(1, 4)], [])]
    if not last:
        devices.append(FakeDevice(
            'z.kept', [FakeProp(2, 1), FakeProp(2, 2)], []))
    poller = MIoTCloudPoller(
        client, devices, {device.did for device in devices}, batch_size=2)
    poller.start()
    loop.call_later.call_args.args[1]()
    task = poller._task
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        poller.remove_device('a.removed')
        assert not task.done()
        assert poller.active is not last
        if outcome == 'stop':
            await asyncio.wait_for(poller.stop(), timeout=5)
            assert task.cancelled()
            assert len(client.calls) == 1
            assert loop.call_later.call_count == 1
        else:
            release.set()
            await asyncio.wait_for(task, timeout=5)
            expected = [] if last else [
                {'did': 'z.kept', 'siid': 2, 'piid': piid}
                for piid in [1, 2]]
            assert [param for batch in client.calls[1:]
                    for param in batch] == expected
            assert all(client.calls)
            assert loop.call_later.call_count == (1 if last else 2)
            client.calls.clear()
            assert await poller.async_poll_once() == len(expected)
            assert [param for batch in client.calls
                    for param in batch] == expected
        assert not poller._poll_lock.locked()
    finally:
        release.set()
        await poller.stop()
