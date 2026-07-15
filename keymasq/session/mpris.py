from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any, cast

from dbus_next.constants import MessageType
from dbus_next.errors import AuthError, DBusError, InvalidAddressError
from dbus_next.message import Message
from dbus_next.signature import Variant

from keymasq.common.model.actions import (
    MPRIS_COMMAND_NEXT,
    MPRIS_COMMAND_PAUSE,
    MPRIS_COMMAND_PLAY,
    MPRIS_COMMAND_PLAY_PAUSE,
    MPRIS_COMMAND_PREVIOUS,
    MPRIS_COMMAND_STOP,
    normalize_mpris_command,
)
from keymasq.session.dbus import DBUS_INTERFACE, DBUS_PATH, DBUS_SERVICE, SessionDBus

type MprisStatusSnapshot = dict[str, object]

MPRIS_SERVICE_PREFIX = "org.mpris.MediaPlayer2"
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER_INTERFACE = "org.mpris.MediaPlayer2.Player"
DBUS_PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
PLAYBACK_STATUS_PROPERTY = "PlaybackStatus"
METADATA_PROPERTY = "Metadata"
CAN_GO_NEXT_PROPERTY = "CanGoNext"
CAN_GO_PREVIOUS_PROPERTY = "CanGoPrevious"
CAN_PLAY_PROPERTY = "CanPlay"
TRACKED_PLAYER_PROPERTIES = frozenset(
    {
        PLAYBACK_STATUS_PROPERTY,
        METADATA_PROPERTY,
        CAN_GO_NEXT_PROPERTY,
        CAN_GO_PREVIOUS_PROPERTY,
        CAN_PLAY_PROPERTY,
    }
)
XESAM_TITLE = "xesam:title"
XESAM_ARTIST = "xesam:artist"
XESAM_ALBUM = "xesam:album"
PLAYING_STATUS = "Playing"
STOPPED_STATUS = "Stopped"
PAUSED_STATUS = "Paused"
DBUS_CALL_TIMEOUT_S = 0.8

log = logging.getLogger("keymasq-session.mpris")


class MprisDBusError(RuntimeError):
    def __init__(self, error_name: str, message: str) -> None:
        super().__init__(message)
        self.error_name = error_name


@dataclass
class MprisPlayerState:
    service: str
    owner: str
    playback_status: str = STOPPED_STATUS
    can_go_next: bool | None = None
    can_go_previous: bool | None = None
    can_play: bool | None = None
    track_title: str | None = None
    track_artists: tuple[str, ...] = field(default_factory=tuple)
    track_album: str | None = None

    @property
    def is_playing(self) -> bool:
        return self.playback_status == PLAYING_STATUS


class MprisController:
    def __init__(self, dbus: SessionDBus) -> None:
        self.dbus = dbus
        self._bus: Any | None = None
        self._started = False
        self._handler_registered = False
        self._match_rules_added: set[str] = set()
        self._start_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._players: dict[str, MprisPlayerState] = {}
        self._service_to_owner: dict[str, str] = {}
        self._player_order: list[str] = []
        self._started_order: list[str] = []
        self._inactive_order: list[str] = []

    async def start(self) -> None:
        async with self._start_lock:
            if self._bus_is_current():
                return
            await self._stop_locked()

            log.debug("Starting MPRIS controller")
            try:
                bus = await self._connect_bus()
                bus.add_message_handler(self._handle_message)
                self._bus = bus
                self._handler_registered = True
                for rule in self._match_rules():
                    await self._call_dbus_daemon("AddMatch", "s", [rule])
                    self._match_rules_added.add(rule)
                self._started = True
                await self.resync()
                log.debug(
                    "MPRIS controller ready: players=%s inactive_order=%s",
                    self._player_states_for_log(),
                    self._inactive_order,
                )
            except MprisDBusError:
                await self._stop_locked()
                await self._disconnect_bus()
                raise

    async def stop(self) -> None:
        async with self._start_lock:
            await self._stop_locked()

    async def resync(self) -> None:
        names = await self._list_names()
        services = sorted(name for name in names if name.startswith(MPRIS_SERVICE_PREFIX))
        seen_owners: set[str] = set()
        seen_services: set[str] = set()
        log.debug("MPRIS resync discovered services: %s", services)

        for service in services:
            owner = await self._get_name_owner(service)
            if not owner:
                continue
            if await self._register_or_refresh_player(service, owner):
                seen_owners.add(owner)
                seen_services.add(service)

        for service in set(self._service_to_owner) - seen_services:
            self._service_to_owner.pop(service, None)
        for owner in set(self._players) - seen_owners:
            self._remove_player(owner)
        log.debug(
            "MPRIS resync complete: players=%s inactive_order=%s",
            self._player_states_for_log(),
            self._inactive_order,
        )

    async def handle_command(self, command: object, *, raise_on_error: bool = False) -> bool:
        normalized = normalize_mpris_command(command)
        async with self._command_lock:
            try:
                await self.start()
                log.debug(
                    "MPRIS command requested: %s players=%s",
                    normalized,
                    self._player_states_for_log(),
                )
                await self._execute_command(normalized, raise_on_error=raise_on_error)
            except MprisDBusError:
                log.exception("MPRIS command failed: %s", normalized)
                await self._reset_after_bus_error()
                if raise_on_error:
                    raise
                return False
        return True

    async def _execute_command(self, command: str, *, raise_on_error: bool) -> None:
        playing = self._playing_players()

        if command == MPRIS_COMMAND_PLAY_PAUSE:
            if playing:
                log.debug("MPRIS play_pause pausing players: %s", playing)
                await self._call_player_methods(
                    playing,
                    "Pause",
                    PAUSED_STATUS,
                    raise_on_error=raise_on_error,
                )
            else:
                player = self._latest_player_for(
                    capability="can_play",
                    prefer_started=True,
                    require_not_playing=True,
                )
                if player:
                    log.debug("MPRIS play_pause resuming inactive player: %s", player)
                    await self._call_player_methods(
                        [player],
                        "Play",
                        PLAYING_STATUS,
                        raise_on_error=raise_on_error,
                    )
                else:
                    log.info("MPRIS play_pause ignored: no known players")
            return

        if command == MPRIS_COMMAND_PAUSE:
            if not playing:
                log.info("MPRIS pause ignored: no playing players")
            else:
                log.debug("MPRIS pause targeting players: %s", playing)
            await self._call_player_methods(
                playing,
                "Pause",
                PAUSED_STATUS,
                raise_on_error=raise_on_error,
            )
            return

        if command == MPRIS_COMMAND_PLAY:
            player = self._latest_player_for(capability="can_play", prefer_started=True)
            if player:
                log.debug("MPRIS play targeting player: %s", player)
                await self._call_player_methods(
                    [player],
                    "Play",
                    PLAYING_STATUS,
                    raise_on_error=raise_on_error,
                )
            else:
                log.info("MPRIS play ignored: no known player supports Play")
            return

        if command in {MPRIS_COMMAND_NEXT, MPRIS_COMMAND_PREVIOUS}:
            member = "Next" if command == MPRIS_COMMAND_NEXT else "Previous"
            capability = "can_go_next" if command == MPRIS_COMMAND_NEXT else "can_go_previous"
            player = self._latest_player_for(capability=capability)
            if player:
                log.debug("MPRIS %s targeting player: %s", command, player)
                await self._call_player_methods(
                    [player],
                    member,
                    None,
                    raise_on_error=raise_on_error,
                )
            else:
                log.info(
                    "MPRIS %s ignored: no known player supports %s",
                    command,
                    member,
                )
            return

        if command == MPRIS_COMMAND_STOP:
            if not playing:
                log.info("MPRIS stop ignored: no playing players")
            else:
                log.debug("MPRIS stop targeting players: %s", playing)
            await self._call_player_methods(
                playing,
                "Stop",
                STOPPED_STATUS,
                raise_on_error=raise_on_error,
            )

    async def _register_or_refresh_player(self, service: str, owner: str) -> bool:
        try:
            properties = await self._get_player_properties(owner)
        except MprisDBusError as exc:
            log.debug("Ignoring MPRIS player %s: %s", service, exc)
            self._remove_player(owner, service=service)
            return False
        status = _normalize_playback_status(properties.get(PLAYBACK_STATUS_PROPERTY))

        previous_owner = self._service_to_owner.get(service)
        if previous_owner is not None and previous_owner != owner:
            self._remove_player(previous_owner, service=service)

        existing = self._players.get(owner)
        if existing is None:
            self._players[owner] = MprisPlayerState(service=service, owner=owner)
            self._mark_player_recent(owner)
            log.debug(
                "MPRIS player added: service=%s owner=%s playback=%s",
                service,
                owner,
                status,
            )
        elif existing.service != service:
            log.debug(
                "MPRIS player service changed: owner=%s service=%s->%s playback=%s",
                owner,
                existing.service,
                service,
                status,
            )
            existing.service = service
            self._mark_player_recent(owner)
        self._update_player_capabilities(owner, properties)
        self._update_player_metadata(owner, properties.get(METADATA_PROPERTY))
        self._service_to_owner[service] = owner
        self._set_playback_status(owner, status)
        return True

    async def _call_player_methods(
        self,
        players: list[str],
        member: str,
        status_after_success: str | None,
        *,
        raise_on_error: bool,
    ) -> None:
        results = await asyncio.gather(
            *(self._call_player_method(player, member) for player in players),
            return_exceptions=True,
        )
        failures: list[MprisDBusError] = []
        for player, result in zip(players, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, MprisDBusError):
                self._handle_player_call_failure(player, member, result)
                failures.append(result)
                continue
            if isinstance(result, Exception):
                raise result
            if status_after_success is not None:
                self._set_playback_status(player, status_after_success)
        if raise_on_error and failures:
            raise _combine_player_failures(member, failures)

    async def _call_player_method(self, player: str, member: str) -> None:
        state = self._players.get(player)
        service = state.service if state is not None else ""
        log.debug("MPRIS DBus call: owner=%s service=%s member=%s", player, service, member)
        await self._call(
            destination=player,
            path=MPRIS_PATH,
            interface=MPRIS_PLAYER_INTERFACE,
            member=member,
        )
        log.debug(
            "MPRIS DBus call succeeded: owner=%s service=%s member=%s",
            player,
            service,
            member,
        )

    def _handle_player_call_failure(
        self,
        player: str,
        member: str,
        exc: MprisDBusError,
    ) -> None:
        log.warning("MPRIS %s call failed for %s: %s", member, player, exc)
        if exc.error_name in {
            "org.freedesktop.DBus.Error.NameHasNoOwner",
            "org.freedesktop.DBus.Error.ServiceUnknown",
            "org.freedesktop.DBus.Error.UnknownObject",
        }:
            self._remove_player(player)

    async def _get_player_properties(self, player: str) -> dict[str, object]:
        body = await self._call(
            destination=player,
            path=MPRIS_PATH,
            interface=DBUS_PROPERTIES_INTERFACE,
            member="GetAll",
            signature="s",
            body=[MPRIS_PLAYER_INTERFACE],
        )
        if not body:
            return {}
        value = body[0]
        if not isinstance(value, dict):
            return {}
        return {
            str(name): _variant_value(raw_value)
            for name, raw_value in cast(dict[object, object], value).items()
        }

    async def _list_names(self) -> list[str]:
        body = await self._call_dbus_daemon("ListNames")
        names_value: object = body[0] if body else []
        if not isinstance(names_value, list):
            return []
        return [str(name) for name in cast(list[object], names_value)]

    async def _get_name_owner(self, service: str) -> str | None:
        try:
            body = await self._call_dbus_daemon("GetNameOwner", "s", [service])
        except MprisDBusError as exc:
            log.debug("MPRIS owner lookup failed for %s: %s", service, exc)
            return None
        if not body:
            return None
        return str(body[0] or "") or None

    async def _call_dbus_daemon(
        self,
        member: str,
        signature: str = "",
        body: list[object] | None = None,
    ) -> list[Any]:
        return await self._call(
            destination=DBUS_SERVICE,
            path=DBUS_PATH,
            interface=DBUS_INTERFACE,
            member=member,
            signature=signature,
            body=body,
        )

    async def _call(
        self,
        *,
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: list[object] | None = None,
    ) -> list[Any]:
        bus = self._bus
        if bus is None:
            raise RuntimeError("MPRIS controller has no session D-Bus connection")
        message = Message(
            destination=destination,
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=body or [],
        )
        reply = await self._call_bus(bus, message, destination=destination, member=member)
        if reply is None:
            raise MprisDBusError("", f"{destination}.{member} returned no reply")
        if reply.message_type == MessageType.ERROR:
            message = str(reply.body[0]) if reply.body else f"{destination}.{member} failed"
            raise MprisDBusError(str(reply.error_name or ""), message)
        return list(reply.body or [])

    async def _connect_bus(self) -> Any:
        try:
            return await self.dbus.bus()
        except InvalidAddressError as exc:
            raise MprisDBusError("", f"invalid session D-Bus address: {exc}") from exc
        except AuthError as exc:
            raise MprisDBusError("", f"session D-Bus authentication failed: {exc}") from exc
        except DBusError as exc:
            raise MprisDBusError("", f"session D-Bus connection failed: {exc}") from exc
        except OSError as exc:
            raise MprisDBusError("", f"session D-Bus transport failed: {exc}") from exc

    async def _call_bus(
        self,
        bus: Any,
        message: Message,
        *,
        destination: str,
        member: str,
    ) -> Message | None:
        try:
            return await asyncio.wait_for(
                bus.call(message),
                timeout=DBUS_CALL_TIMEOUT_S,
            )
        except TimeoutError as exc:
            raise MprisDBusError("", f"{destination}.{member} timed out") from exc
        except DBusError as exc:
            raise MprisDBusError("", f"{destination}.{member} failed: {exc}") from exc
        except OSError as exc:
            raise MprisDBusError("", f"{destination}.{member} transport failed: {exc}") from exc

    def _handle_message(self, message: Message) -> bool:
        if message.message_type != MessageType.SIGNAL:
            return False
        if (
            message.interface == DBUS_INTERFACE
            and message.path == DBUS_PATH
            and message.member == "NameOwnerChanged"
        ):
            self._handle_name_owner_changed_signal(message)
            return False
        if (
            message.interface == DBUS_PROPERTIES_INTERFACE
            and message.path == MPRIS_PATH
            and message.member == "PropertiesChanged"
        ):
            self._handle_properties_changed_signal(message)
        return False

    def _handle_name_owner_changed_signal(self, message: Message) -> None:
        if len(message.body) < 3:
            return
        service = str(message.body[0] or "")
        if not service.startswith(MPRIS_SERVICE_PREFIX):
            return
        old_owner = str(message.body[1] or "")
        new_owner = str(message.body[2] or "")
        self._create_task(
            self._handle_name_owner_changed(service, old_owner, new_owner),
            f"mpris-owner:{service}",
        )

    async def _handle_name_owner_changed(
        self,
        service: str,
        old_owner: str,
        new_owner: str,
    ) -> None:
        log.debug(
            "MPRIS owner changed: service=%s old_owner=%s new_owner=%s",
            service,
            old_owner,
            new_owner,
        )
        if old_owner:
            self._remove_player(old_owner, service=service)
        if new_owner:
            await self._register_or_refresh_player(service, new_owner)

    def _handle_properties_changed_signal(self, message: Message) -> None:
        if len(message.body) < 2:
            return
        interface_name = str(message.body[0] or "")
        if interface_name != MPRIS_PLAYER_INTERFACE:
            return
        changed = message.body[1]
        if not isinstance(changed, dict):
            changed = {}
        sender = str(message.sender or "")
        if sender not in self._players:
            log.debug("Ignoring MPRIS PropertiesChanged from unknown sender: %s", sender)
            return
        changed_properties = {
            str(name): _variant_value(raw_value)
            for name, raw_value in cast(dict[object, object], changed).items()
        }
        if PLAYBACK_STATUS_PROPERTY in changed_properties:
            self._set_playback_status(
                sender,
                str(changed_properties.get(PLAYBACK_STATUS_PROPERTY) or STOPPED_STATUS),
                observed=True,
            )
        self._update_player_capabilities(sender, changed_properties)
        if METADATA_PROPERTY in changed_properties:
            self._update_player_metadata(sender, changed_properties.get(METADATA_PROPERTY))
        invalidated = _invalidated_properties(message.body[2] if len(message.body) > 2 else [])
        if invalidated & TRACKED_PLAYER_PROPERTIES:
            player = self._players.get(sender)
            if player is not None:
                log.debug(
                    "MPRIS properties invalidated, refreshing player: "
                    "owner=%s service=%s properties=%s",
                    sender,
                    player.service,
                    sorted(invalidated & TRACKED_PLAYER_PROPERTIES),
                )
                self._create_task(
                    self._refresh_player(player.service, sender),
                    f"mpris-refresh:{sender}",
                )

    def _update_player_capabilities(
        self,
        owner: str,
        properties: dict[str, object],
    ) -> None:
        player = self._players.get(owner)
        if player is None:
            return
        if CAN_GO_NEXT_PROPERTY in properties:
            player.can_go_next = _bool_property(properties, CAN_GO_NEXT_PROPERTY)
        if CAN_GO_PREVIOUS_PROPERTY in properties:
            player.can_go_previous = _bool_property(properties, CAN_GO_PREVIOUS_PROPERTY)
        if CAN_PLAY_PROPERTY in properties:
            player.can_play = _bool_property(properties, CAN_PLAY_PROPERTY)

    def _update_player_metadata(self, owner: str, metadata: object) -> None:
        player = self._players.get(owner)
        if player is None:
            return
        metadata_map = _metadata_map(metadata)
        player.track_title = _string_metadata(metadata_map, XESAM_TITLE)
        player.track_artists = tuple(_string_list_metadata(metadata_map, XESAM_ARTIST))
        player.track_album = _string_metadata(metadata_map, XESAM_ALBUM)

    async def _refresh_player(self, service: str, owner: str) -> None:
        await self._register_or_refresh_player(service, owner)

    def _set_playback_status(self, owner: str, status: str, *, observed: bool = False) -> None:
        player = self._players.get(owner)
        if player is None:
            return
        previous_status = player.playback_status
        player.playback_status = _normalize_playback_status(status)
        entered_playing = previous_status != PLAYING_STATUS and player.is_playing
        if previous_status != player.playback_status:
            log.debug(
                "MPRIS playback changed: owner=%s service=%s playback=%s->%s",
                owner,
                player.service,
                previous_status,
                player.playback_status,
            )
        if player.is_playing:
            self._remove_inactive(owner)
            if entered_playing:
                self._mark_started_recent(owner)
        else:
            self._mark_inactive_recent(owner)

    def _playing_players(self) -> list[str]:
        return [owner for owner, player in self._players.items() if player.is_playing]

    def status_snapshot(self) -> MprisStatusSnapshot:
        return {
            "started": bool(self._started),
            "players": [
                {
                    "service": player.service,
                    "owner": player.owner,
                    "playback_status": player.playback_status,
                    "playing": player.is_playing,
                    "can_go_next": player.can_go_next,
                    "can_go_previous": player.can_go_previous,
                    "can_play": player.can_play,
                    "track": {
                        "title": player.track_title,
                        "artists": list(player.track_artists),
                        "album": player.track_album,
                    },
                    "inactive_recent": owner in self._inactive_order,
                }
                for owner, player in sorted(self._players.items())
            ],
            "player_order": list(self._player_order),
            "started_order": list(self._started_order),
            "inactive_order": list(self._inactive_order),
        }

    def _player_states_for_log(self) -> list[str]:
        return [
            f"{player.service}({owner}):{player.playback_status}"
            for owner, player in sorted(self._players.items())
        ]

    def _latest_player_for(
        self,
        *,
        capability: str | None = None,
        prefer_started: bool = False,
        require_not_playing: bool = False,
    ) -> str | None:
        seen: set[str] = set()
        orders = [self._player_order]
        if prefer_started:
            orders.insert(0, self._started_order)
        for order in orders:
            for owner in reversed(order):
                if owner in seen:
                    continue
                seen.add(owner)
                player = self._players.get(owner)
                if player is None:
                    continue
                if require_not_playing and player.is_playing:
                    continue
                if not self._player_supports(owner, capability):
                    continue
                return owner
        return None

    def _player_supports(self, owner: str, capability: str | None) -> bool:
        if capability is None:
            return True
        player = self._players.get(owner)
        if player is None:
            return False
        value = getattr(player, capability)
        return value is not False

    def _mark_player_recent(self, owner: str) -> None:
        self._remove_ordered(self._player_order, owner)
        self._player_order.append(owner)

    def _mark_started_recent(self, owner: str) -> None:
        self._remove_ordered(self._started_order, owner)
        self._started_order.append(owner)

    def _remove_from_orders(self, owner: str) -> None:
        self._remove_ordered(self._player_order, owner)
        self._remove_ordered(self._started_order, owner)

    def _remove_ordered(self, order: list[str], owner: str) -> None:
        with contextlib.suppress(ValueError):
            order.remove(owner)

    def _mark_inactive_recent(self, owner: str) -> None:
        self._remove_inactive(owner)
        self._inactive_order.append(owner)

    def _remove_inactive(self, owner: str) -> None:
        with contextlib.suppress(ValueError):
            self._inactive_order.remove(owner)

    def _remove_player(self, owner: str, *, service: str | None = None) -> None:
        if service is not None:
            self._service_to_owner.pop(service, None)
            remaining_services = sorted(
                service_name
                for service_name, service_owner in self._service_to_owner.items()
                if service_owner == owner
            )
            if remaining_services and owner in self._players:
                self._players[owner].service = remaining_services[0]
                return

        player = self._players.pop(owner, None)
        if player is not None:
            for service_name, service_owner in list(self._service_to_owner.items()):
                if service_owner == owner:
                    self._service_to_owner.pop(service_name, None)
            log.debug(
                "MPRIS player removed: service=%s owner=%s playback=%s",
                player.service,
                owner,
                player.playback_status,
            )
        self._remove_inactive(owner)
        self._remove_from_orders(owner)

    def _create_task(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._handle_task_done)

    def _handle_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            log.debug("MPRIS background task failed", exc_info=(type(exc), exc, exc.__traceback__))

    async def _reset_after_bus_error(self) -> None:
        async with self._start_lock:
            await self._stop_locked()
        await self._disconnect_bus()

    async def _stop_locked(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.difference_update(tasks)

        bus = self._bus
        for rule in list(self._match_rules_added):
            if bus is not None:
                try:
                    await self._call_dbus_daemon("RemoveMatch", "s", [rule])
                except MprisDBusError as exc:
                    log.debug("MPRIS RemoveMatch cleanup failed: %s", exc)
            self._match_rules_added.discard(rule)

        if bus is not None and self._handler_registered:
            with contextlib.suppress(ValueError):
                bus.remove_message_handler(self._handle_message)

        self._bus = None
        self._started = False
        self._handler_registered = False
        self._players.clear()
        self._service_to_owner.clear()
        self._player_order.clear()
        self._started_order.clear()
        self._inactive_order.clear()

    def _bus_is_current(self) -> bool:
        return bool(
            self._started and self._bus is not None and bool(getattr(self._bus, "connected", True))
        )

    async def _disconnect_bus(self) -> None:
        try:
            await self.dbus.disconnect()
        except OSError as exc:
            log.debug("MPRIS D-Bus disconnect failed: %s", exc)

    def _match_rules(self) -> tuple[str, str]:
        return (
            (
                "type='signal',sender='org.freedesktop.DBus',"
                "interface='org.freedesktop.DBus',member='NameOwnerChanged',"
                "path='/org/freedesktop/DBus'"
            ),
            (
                "type='signal',interface='org.freedesktop.DBus.Properties',"
                "member='PropertiesChanged',path='/org/mpris/MediaPlayer2',"
                "arg0='org.mpris.MediaPlayer2.Player'"
            ),
        )


def _normalize_playback_status(status: object) -> str:
    status_str = str(_variant_value(status) or STOPPED_STATUS)
    if status_str in {PLAYING_STATUS, PAUSED_STATUS, STOPPED_STATUS}:
        return status_str
    return STOPPED_STATUS


def _combine_player_failures(member: str, failures: list[MprisDBusError]) -> MprisDBusError:
    if len(failures) == 1:
        return failures[0]
    details = "; ".join(str(failure) for failure in failures)
    return MprisDBusError("", f"{member} failed for {len(failures)} players: {details}")


def _bool_property(properties: dict[str, object], name: str) -> bool | None:
    value = properties.get(name)
    if isinstance(value, bool):
        return value
    return None


def _metadata_map(metadata: object) -> dict[str, object]:
    metadata = _variant_value(metadata)
    if not isinstance(metadata, dict):
        return {}
    return {
        str(name): _variant_value(value)
        for name, value in cast(dict[object, object], metadata).items()
    }


def _string_metadata(metadata: dict[str, object], name: str) -> str | None:
    value = _variant_value(metadata.get(name))
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _string_list_metadata(metadata: dict[str, object], name: str) -> list[str]:
    value = _variant_value(metadata.get(name))
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in cast(list[object], value) if str(item).strip()]


def _invalidated_properties(value: object) -> set[str]:
    value = _variant_value(value)
    if not isinstance(value, list):
        return set()
    return {str(item) for item in cast(list[object], value)}


def _variant_value(value: object) -> object:
    if isinstance(value, Variant):
        return value.value
    return value
