# -*- coding: utf-8 -*-
"""Tests for selected-device cloud property polling."""
import asyncio
from types import SimpleNamespace

import pytest

# pylint: disable=import-outside-toplevel


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
