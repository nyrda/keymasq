from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import pytest
from dbus_next.constants import MessageType
from dbus_next.errors import InvalidAddressError
from dbus_next.message import Message
from dbus_next.signature import Variant

from keymasq.session.dbus import DBUS_INTERFACE, DBUS_PATH, DBUS_SERVICE
from keymasq.session.mpris import (
    CAN_GO_NEXT_PROPERTY,
    CAN_GO_PREVIOUS_PROPERTY,
    CAN_PLAY_PROPERTY,
    DBUS_PROPERTIES_INTERFACE,
    METADATA_PROPERTY,
    MPRIS_PATH,
    MPRIS_PLAYER_INTERFACE,
    PLAYBACK_STATUS_PROPERTY,
    XESAM_ALBUM,
    XESAM_ARTIST,
    XESAM_TITLE,
    MprisController,
    MprisDBusError,
)


def _reply(body: list[object]) -> Message:
    return Message(
        message_type=MessageType.METHOD_RETURN,
        reply_serial=1,
        body=body,
    )


def _error_reply(error_name: str, message: str) -> Message:
    return Message(
        message_type=MessageType.ERROR,
        error_name=error_name,
        reply_serial=1,
        signature="s",
        body=[message],
    )


class _FakeBus:
    connected = True

    def __init__(
        self,
        players: dict[str, tuple[str, str]],
        capabilities: dict[str, dict[str, bool]] | None = None,
        metadata: dict[str, dict[str, object]] | None = None,
        method_errors: dict[tuple[str, str], Message] | None = None,
    ) -> None:
        self.players = dict(players)
        self.capabilities = capabilities or {}
        self.metadata = metadata or {}
        self.method_errors = method_errors or {}
        self.handlers: list[Any] = []
        self.player_calls: list[tuple[str, str]] = []
        self.match_rules: list[str] = []

    def add_message_handler(self, handler) -> None:
        self.handlers.append(handler)

    def remove_message_handler(self, handler) -> None:
        self.handlers.remove(handler)

    async def call(self, message: Message) -> Message:
        if message.destination == DBUS_SERVICE:
            return self._handle_dbus_call(message)
        if (
            message.path == MPRIS_PATH
            and message.interface == DBUS_PROPERTIES_INTERFACE
            and message.member == "GetAll"
        ):
            service = self._service_for_destination(str(message.destination))
            status = self.players[service][1]
            capabilities = self.capabilities.get(service, {})
            metadata = self.metadata.get(service, {})
            return _reply(
                [
                    {
                        PLAYBACK_STATUS_PROPERTY: Variant("s", status),
                        METADATA_PROPERTY: Variant(
                            "a{sv}",
                            {
                                name: _metadata_variant(value)
                                for name, value in metadata.items()
                            },
                        ),
                        CAN_GO_NEXT_PROPERTY: Variant(
                            "b",
                            capabilities.get(CAN_GO_NEXT_PROPERTY, True),
                        ),
                        CAN_GO_PREVIOUS_PROPERTY: Variant(
                            "b",
                            capabilities.get(CAN_GO_PREVIOUS_PROPERTY, True),
                        ),
                        CAN_PLAY_PROPERTY: Variant("b", capabilities.get(CAN_PLAY_PROPERTY, True)),
                    }
                ]
            )
        if message.path == MPRIS_PATH and message.interface == MPRIS_PLAYER_INTERFACE:
            destination = str(message.destination)
            service = self._service_for_destination(destination)
            member = str(message.member)
            self.player_calls.append((destination, member))
            error = self.method_errors.get((destination, member))
            if error is not None:
                return error
            owner, status = self.players[service]
            if member == "Pause":
                status = "Paused"
            elif member == "Play":
                status = "Playing"
            elif member == "Stop":
                status = "Stopped"
            self.players[service] = (owner, status)
            return _reply([])
        raise AssertionError(f"unexpected D-Bus call: {message}")

    def _service_for_destination(self, destination: str) -> str:
        if destination in self.players:
            return destination
        for service, (owner, _status) in self.players.items():
            if owner == destination:
                return service
        raise AssertionError(f"unknown MPRIS destination: {destination}")

    def _handle_dbus_call(self, message: Message) -> Message:
        if message.member == "ListNames":
            return _reply([list(self.players)])
        if message.member == "GetNameOwner":
            service = str(message.body[0])
            return _reply([self.players[service][0]])
        if message.member == "AddMatch":
            self.match_rules.append(str(message.body[0]))
            return _reply([])
        if message.member == "RemoveMatch":
            rule = str(message.body[0])
            if rule in self.match_rules:
                self.match_rules.remove(rule)
            return _reply([])
        raise AssertionError(f"unexpected D-Bus daemon call: {message.member}")

    def emit(self, message: Message) -> None:
        for handler in list(self.handlers):
            handler(message)


class _FakeDBus:
    def __init__(self, bus: _FakeBus) -> None:
        self._bus = bus
        self.disconnect_calls = 0

    async def bus(self) -> _FakeBus:
        return self._bus

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


class _FailingDBus:
    def __init__(self) -> None:
        self.disconnect_calls = 0

    async def bus(self) -> object:
        raise InvalidAddressError("bad session bus")

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


def _controller(
    players: dict[str, tuple[str, str]],
    capabilities: dict[str, dict[str, bool]] | None = None,
    metadata: dict[str, dict[str, object]] | None = None,
    method_errors: dict[tuple[str, str], Message] | None = None,
) -> tuple[MprisController, _FakeBus]:
    bus = _FakeBus(players, capabilities, metadata, method_errors)
    return MprisController(_FakeDBus(bus)), bus  # type: ignore[arg-type]


def _metadata_variant(value: object) -> Variant:
    if isinstance(value, list):
        return Variant("as", [str(item) for item in value])
    return Variant("s", str(value))


@pytest.mark.asyncio
async def test_mpris_play_pause_pauses_playing_then_resumes_latest_started_player() -> None:
    controller, bus = _controller(
        {
            "org.mpris.MediaPlayer2.alpha": (":1.10", "Playing"),
            "org.mpris.MediaPlayer2.beta": (":1.11", "Paused"),
        }
    )
    await controller.start()

    await controller.handle_command("play_pause")
    await controller.handle_command("play_pause")

    assert bus.player_calls == [
        (":1.10", "Pause"),
        (":1.10", "Play"),
    ]


@pytest.mark.asyncio
async def test_mpris_play_pause_pauses_every_playing_player() -> None:
    controller, bus = _controller(
        {
            "org.mpris.MediaPlayer2.alpha": (":1.10", "Playing"),
            "org.mpris.MediaPlayer2.beta": (":1.11", "Playing"),
            "org.mpris.MediaPlayer2.gamma": (":1.12", "Paused"),
        }
    )
    await controller.start()

    await controller.handle_command("play_pause")

    assert bus.player_calls == [
        (":1.10", "Pause"),
        (":1.11", "Pause"),
    ]


@pytest.mark.asyncio
async def test_mpris_play_pause_resumes_latest_user_started_player_after_pause_all() -> None:
    controller, bus = _controller(
        {
            "org.mpris.MediaPlayer2.firefox": (":1.20", "Paused"),
            "org.mpris.MediaPlayer2.spotify": (":1.10", "Playing"),
        }
    )
    await controller.start()

    bus.emit(
        Message(
            path=MPRIS_PATH,
            interface=DBUS_PROPERTIES_INTERFACE,
            member="PropertiesChanged",
            message_type=MessageType.SIGNAL,
            sender=":1.20",
            body=[
                MPRIS_PLAYER_INTERFACE,
                {PLAYBACK_STATUS_PROPERTY: Variant("s", "Playing")},
                [],
            ],
        )
    )

    await controller.handle_command("play_pause")
    await controller.handle_command("play_pause")

    assert bus.player_calls == [
        (":1.20", "Pause"),
        (":1.10", "Pause"),
        (":1.20", "Play"),
    ]


@pytest.mark.asyncio
async def test_mpris_unchanged_playing_refresh_does_not_promote_started_order() -> None:
    controller, bus = _controller(
        {
            "org.mpris.MediaPlayer2.alpha": (":1.10", "Playing"),
            "org.mpris.MediaPlayer2.beta": (":1.11", "Playing"),
        }
    )
    await controller.start()

    bus.emit(
        Message(
            path=MPRIS_PATH,
            interface=DBUS_PROPERTIES_INTERFACE,
            member="PropertiesChanged",
            message_type=MessageType.SIGNAL,
            sender=":1.10",
            body=[
                MPRIS_PLAYER_INTERFACE,
                {PLAYBACK_STATUS_PROPERTY: Variant("s", "Playing")},
                [],
            ],
        )
    )

    await controller.handle_command("play_pause")
    await controller.handle_command("play_pause")

    assert bus.player_calls == [
        (":1.10", "Pause"),
        (":1.11", "Pause"),
        (":1.11", "Play"),
    ]


@pytest.mark.asyncio
async def test_mpris_play_targets_latest_detected_player_without_started_history() -> None:
    controller, bus = _controller(
        {
            "org.mpris.MediaPlayer2.alpha": (":1.10", "Paused"),
            "org.mpris.MediaPlayer2.beta": (":1.11", "Stopped"),
        }
    )
    await controller.start()

    await controller.handle_command("play")

    assert bus.player_calls == [(":1.11", "Play")]


@pytest.mark.asyncio
async def test_mpris_next_targets_latest_detected_player_even_with_multiple_playing() -> None:
    controller, bus = _controller(
        {
            "org.mpris.MediaPlayer2.alpha": (":1.10", "Playing"),
            "org.mpris.MediaPlayer2.beta": (":1.11", "Playing"),
            "org.mpris.MediaPlayer2.gamma": (":1.12", "Paused"),
        }
    )
    await controller.start()

    await controller.handle_command("next")
    await controller.handle_command("pause")
    await controller.handle_command("next")

    assert bus.player_calls == [
        (":1.12", "Next"),
        (":1.10", "Pause"),
        (":1.11", "Pause"),
        (":1.12", "Next"),
    ]


@pytest.mark.asyncio
async def test_mpris_next_uses_latest_detected_player_with_capability_fallback() -> None:
    controller, bus = _controller(
        {
            "org.mpris.MediaPlayer2.alpha": (":1.10", "Paused"),
            "org.mpris.MediaPlayer2.beta": (":1.11", "Paused"),
        },
        capabilities={
            "org.mpris.MediaPlayer2.beta": {CAN_GO_NEXT_PROPERTY: False},
        },
    )
    await controller.start()

    await controller.handle_command("next")

    assert bus.player_calls == [(":1.10", "Next")]


@pytest.mark.asyncio
async def test_mpris_invalidated_capabilities_refresh_player_properties() -> None:
    service = "org.mpris.MediaPlayer2.alpha"
    controller, bus = _controller(
        {service: (":1.10", "Paused")},
        capabilities={service: {CAN_GO_NEXT_PROPERTY: False}},
    )
    await controller.start()

    await controller.handle_command("next")
    assert bus.player_calls == []

    bus.capabilities[service] = {CAN_GO_NEXT_PROPERTY: True}
    bus.emit(
        Message(
            path=MPRIS_PATH,
            interface=DBUS_PROPERTIES_INTERFACE,
            member="PropertiesChanged",
            message_type=MessageType.SIGNAL,
            sender=":1.10",
            body=[
                MPRIS_PLAYER_INTERFACE,
                {},
                [CAN_GO_NEXT_PROPERTY],
            ],
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await controller.handle_command("next")

    assert bus.player_calls == [(":1.10", "Next")]


@pytest.mark.asyncio
async def test_mpris_playback_changes_do_not_promote_detected_order() -> None:
    controller, bus = _controller(
        {
            "org.mpris.MediaPlayer2.alpha": (":1.10", "Paused"),
            "org.mpris.MediaPlayer2.beta": (":1.11", "Paused"),
        }
    )
    await controller.start()

    bus.emit(
        Message(
            path=MPRIS_PATH,
            interface=DBUS_PROPERTIES_INTERFACE,
            member="PropertiesChanged",
            message_type=MessageType.SIGNAL,
            sender=":1.10",
            body=[
                MPRIS_PLAYER_INTERFACE,
                {PLAYBACK_STATUS_PROPERTY: Variant("s", "Playing")},
                [],
            ],
        )
    )

    await controller.handle_command("next")

    assert bus.player_calls == [(":1.11", "Next")]


@pytest.mark.asyncio
async def test_mpris_command_handles_dbus_next_connect_errors() -> None:
    dbus = _FailingDBus()
    controller = MprisController(dbus)  # type: ignore[arg-type]

    await controller.handle_command("pause")

    assert dbus.disconnect_calls >= 1


@pytest.mark.asyncio
async def test_mpris_command_can_raise_dbus_errors_for_direct_commands() -> None:
    dbus = _FailingDBus()
    controller = MprisController(dbus)  # type: ignore[arg-type]

    with pytest.raises(MprisDBusError):
        await controller.handle_command("pause", raise_on_error=True)

    assert dbus.disconnect_calls >= 1


@pytest.mark.asyncio
async def test_mpris_direct_command_raises_when_player_method_fails() -> None:
    controller, bus = _controller(
        {"org.mpris.MediaPlayer2.alpha": (":1.10", "Playing")},
        method_errors={
            (":1.10", "Pause"): _error_reply(
                "org.freedesktop.DBus.Error.Failed",
                "pause failed",
            )
        },
    )
    await controller.start()

    with pytest.raises(MprisDBusError, match="pause failed"):
        await controller.handle_command("pause", raise_on_error=True)

    assert bus.player_calls == [(":1.10", "Pause")]


@pytest.mark.asyncio
async def test_mpris_controller_tracks_new_players_and_properties_changed() -> None:
    controller, bus = _controller({})
    await controller.start()

    bus.players["org.mpris.MediaPlayer2.alpha"] = (":1.10", "Paused")
    bus.emit(
        Message(
            path=DBUS_PATH,
            interface=DBUS_INTERFACE,
            member="NameOwnerChanged",
            message_type=MessageType.SIGNAL,
            body=["org.mpris.MediaPlayer2.alpha", "", ":1.10"],
        )
    )
    await asyncio.sleep(0)

    bus.emit(
        Message(
            path=MPRIS_PATH,
            interface=DBUS_PROPERTIES_INTERFACE,
            member="PropertiesChanged",
            message_type=MessageType.SIGNAL,
            sender=":1.10",
            body=[
                MPRIS_PLAYER_INTERFACE,
                {"PlaybackStatus": Variant("s", "Playing")},
                [],
            ],
        )
    )
    await controller.handle_command("pause")

    assert bus.player_calls == [(":1.10", "Pause")]


@pytest.mark.asyncio
async def test_mpris_controller_tracks_current_metadata() -> None:
    service = "org.mpris.MediaPlayer2.spotify"
    controller, bus = _controller(
        {service: (":1.10", "Playing")},
        metadata={
            service: {
                XESAM_TITLE: "Initial Song",
                XESAM_ARTIST: ["First Artist"],
                XESAM_ALBUM: "Initial Album",
            }
        },
    )
    await controller.start()

    snapshot = controller.status_snapshot()
    players = cast(list[object], snapshot["players"])
    player = players[0]
    assert isinstance(player, dict)
    assert player["track"] == {
        "title": "Initial Song",
        "artists": ["First Artist"],
        "album": "Initial Album",
    }
    assert player["can_play"] is True
    assert player["can_go_next"] is True
    assert player["can_go_previous"] is True

    bus.emit(
        Message(
            path=MPRIS_PATH,
            interface=DBUS_PROPERTIES_INTERFACE,
            member="PropertiesChanged",
            message_type=MessageType.SIGNAL,
            sender=":1.10",
            body=[
                MPRIS_PLAYER_INTERFACE,
                {
                    METADATA_PROPERTY: Variant(
                        "a{sv}",
                        {
                            XESAM_TITLE: Variant("s", "Updated Song"),
                            XESAM_ARTIST: Variant("as", ["Second Artist"]),
                        },
                    )
                },
                [],
            ],
        )
    )

    snapshot = controller.status_snapshot()
    players = cast(list[object], snapshot["players"])
    player = players[0]
    assert isinstance(player, dict)
    assert player["track"] == {
        "title": "Updated Song",
        "artists": ["Second Artist"],
        "album": None,
    }
    assert player["can_play"] is True
    assert player["can_go_next"] is True
    assert player["can_go_previous"] is True


@pytest.mark.asyncio
async def test_mpris_verbose_logging_tracks_players_and_actions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="keymasq-session.mpris")
    service = "org.mpris.MediaPlayer2.alpha"
    controller, bus = _controller({service: (":1.10", "Paused")})

    await controller.start()
    bus.emit(
        Message(
            path=MPRIS_PATH,
            interface=DBUS_PROPERTIES_INTERFACE,
            member="PropertiesChanged",
            message_type=MessageType.SIGNAL,
            sender=":1.10",
            body=[
                MPRIS_PLAYER_INTERFACE,
                {"PlaybackStatus": Variant("s", "Playing")},
                [],
            ],
        )
    )
    await controller.handle_command("stop")

    messages = "\n".join(caplog.messages)
    assert f"MPRIS player added: service={service} owner=:1.10 playback=Paused" in messages
    assert (
        f"MPRIS playback changed: owner=:1.10 service={service} playback=Paused->Playing"
        in messages
    )
    assert "MPRIS command requested: stop" in messages
    assert "MPRIS stop targeting players: [':1.10']" in messages
    assert f"MPRIS DBus call: owner=:1.10 service={service} member=Stop" in messages
    assert f"MPRIS DBus call succeeded: owner=:1.10 service={service} member=Stop" in messages
