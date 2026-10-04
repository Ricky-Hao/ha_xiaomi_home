# -*- coding: utf-8 -*-
"""
Copyright (C) 2024 Xiaomi Corporation.

The ownership and intellectual property rights of Xiaomi Home Assistant
Integration and related Xiaomi cloud service API interface provided under this
license, including source code and object code (collectively, "Licensed Work"),
are owned by Xiaomi. Subject to the terms and conditions of this License, Xiaomi
hereby grants you a personal, limited, non-exclusive, non-transferable,
non-sublicensable, and royalty-free license to reproduce, use, modify, and
distribute the Licensed Work only for your use of Home Assistant for
non-commercial purposes. For the avoidance of doubt, Xiaomi does not authorize
you to use the Licensed Work for any other purpose, including but not limited
to use Licensed Work to develop applications (APP), Web services, and other
forms of software.

You may reproduce and distribute copies of the Licensed Work, with or without
modifications, whether in source or object form, provided that you must give
any other recipients of the Licensed Work a copy of this License and retain all
copyright and disclaimers.

Xiaomi provides the Licensed Work on an "AS IS" BASIS WITHOUT WARRANTIES OR
CONDITIONS OF ANY KIND, either express or implied, including, without
limitation, any warranties, undertakes, or conditions of TITLE, NO ERROR OR
OMISSION, CONTINUITY, RELIABILITY, NON-INFRINGEMENT, MERCHANTABILITY, or
FITNESS FOR A PARTICULAR PURPOSE. In any event, you are solely responsible
for any direct, indirect, special, incidental, or consequential damages or
losses arising from the use or inability to use the Licensed Work.

Xiaomi reserves all rights not expressly granted to you in this License.
Except for the rights expressly granted by Xiaomi under this License, Xiaomi
does not authorize you in any form to use the trademarks, copyrights, or other
forms of intellectual property rights of Xiaomi and its affiliates, including,
without limitation, without obtaining other written permission from Xiaomi, you
shall not use "Xiaomi", "Mijia" and other words related to Xiaomi or words that
may make the public associate with Xiaomi in any form to publicize or promote
the software or hardware devices that use the Licensed Work.

Xiaomi has the right to immediately terminate all your authorization under this
License in the event:
1. You assert patent invalidation, litigation, or other claims against patents
or other intellectual property rights of Xiaomi or its affiliates; or,
2. You make, have made, manufacture, sell, or offer to sell products that knock
off Xiaomi or its affiliates' products.

Cloud property polling for selected devices.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

# pylint: disable=relative-beyond-top-level
from .const import (
    CLOUD_POLL_PROP_BATCH_SIZE,
    DEFAULT_CLOUD_POLL_INTERVAL,
    MAX_CLOUD_POLL_INTERVAL,
    MIN_CLOUD_POLL_INTERVAL,
)
from .miot_error import MIoTError

if TYPE_CHECKING:
    from .miot_client import MIoTClient
    from .miot_device import MIoTDevice

_LOGGER = logging.getLogger(__name__)


class MIoTCloudPoller:
    """Periodically refresh selected device properties from Xiaomi Cloud."""

    def __init__(
        self,
        miot_client: MIoTClient,
        devices: list[MIoTDevice],
        selected_dids: set[str],
        interval: int = DEFAULT_CLOUD_POLL_INTERVAL,
        batch_size: int = CLOUD_POLL_PROP_BATCH_SIZE,
    ) -> None:
        if not MIN_CLOUD_POLL_INTERVAL <= interval <= MAX_CLOUD_POLL_INTERVAL:
            raise ValueError(f'invalid cloud poll interval, {interval}')
        if batch_size <= 0:
            raise ValueError(f'invalid cloud poll batch size, {batch_size}')
        self._miot_client = miot_client
        self._main_loop = miot_client.main_loop
        self._interval = interval
        self._batch_size = batch_size
        self._params = self.__get_poll_params(
            devices=devices, selected_dids=selected_dids)
        self._poll_lock = asyncio.Lock()
        self._timer: Optional[asyncio.TimerHandle] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._active = False

    @property
    def active(self) -> bool:
        """Return whether the poller is running."""
        return self._active

    @property
    def prop_count(self) -> int:
        """Return the number of properties included in each polling round."""
        return len(self._params)

    def start(self) -> bool:
        """Start periodic polling."""
        if self._active:
            return True
        if not self._params:
            _LOGGER.warning('cloud poll disabled, no readable entity property')
            return False
        self._active = True
        self.__schedule()
        _LOGGER.info(
            'cloud poll started, interval=%s, properties=%s',
            self._interval, len(self._params))
        return True

    async def stop(self) -> None:
        """Stop periodic polling and cancel the active polling round."""
        self._active = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        task = self._task
        if task and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def async_poll_once(self) -> int:
        """Run one cloud polling round without overlapping another round."""
        if self._poll_lock.locked():
            _LOGGER.warning(
                'cloud poll skipped, previous round is still active')
            return 0
        refreshed_count = 0
        async with self._poll_lock:
            for index in range(0, len(self._params), self._batch_size):
                try:
                    refreshed = (
                        await self._miot_client.refresh_cloud_props_async(
                            self._params[index:index+self._batch_size]))
                except MIoTError as err:
                    _LOGGER.error(
                        'cloud poll batch failed, offset=%s, %s', index, err)
                    continue
                except Exception as err:  # pylint: disable=broad-exception-caught
                    _LOGGER.error(
                        'cloud poll batch unexpected error, offset=%s, %s',
                        index, err, exc_info=True)
                    continue
                refreshed_count += len(refreshed)
        return refreshed_count

    def __schedule(self) -> None:
        if not self._active or self._timer:
            return
        self._timer = self._main_loop.call_later(
            self._interval, self.__start_poll)

    def __start_poll(self) -> None:
        self._timer = None
        if not self._active:
            return
        self._task = self._main_loop.create_task(self.__poll_and_schedule())

    async def __poll_and_schedule(self) -> None:
        try:
            await self.async_poll_once()
        except MIoTError as err:
            _LOGGER.error('cloud poll failed, %s', err)
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOGGER.error('cloud poll unexpected error, %s', err, exc_info=True)
        finally:
            self._task = None
            self.__schedule()

    @staticmethod
    def __get_poll_params(
        devices: list[MIoTDevice], selected_dids: set[str]
    ) -> list[dict]:
        params: dict[tuple[str, int, int], dict] = {}
        for device in devices:
            if device.did not in selected_dids:
                continue
            props = []
            for prop_list in device.prop_list.values():
                props.extend(prop_list)
            for entity_list in device.entity_list.values():
                for entity in entity_list:
                    props.extend(entity.props)
            for prop in props:
                if not prop.readable:
                    continue
                key = (device.did, prop.service.iid, prop.iid)
                params[key] = {
                    'did': device.did,
                    'siid': prop.service.iid,
                    'piid': prop.iid,
                }
        return [params[key] for key in sorted(params)]
