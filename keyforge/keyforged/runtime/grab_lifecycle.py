from collections.abc import Sequence
from typing import Any


async def grab_device_unlocked(
    manager: Any,
    hardware_id: str,
    evdev_paths: list[str],
    button_map: dict[str, str],
    button_codes: dict[str, int] | None,
    force_grab_unmapped: bool,
    *,
    update_desired: bool,
    clear_device_path_cache_fn: Any,
    resolve_stable_path_fn: Any,
    primary_input_class_fn: Any,
    grabbed_device_cls: Any,
    log: Any,
    errno_mod: Any,
) -> dict[str, object]:
    clear_device_path_cache_fn()
    manager._cancel_pending_hardware_release(hardware_id)

    requested_paths = {
        resolve_stable_path_fn(str(path)) for path in evdev_paths if str(path or "").strip()
    }
    mapped_evdev_names = {name.lower() for name in button_map.values()}
    resolved_button_codes = {
        button_id: int(code) for button_id, code in (button_codes or {}).items()
    }
    mapped_codes = set(resolved_button_codes.values())
    if update_desired:
        manager._desired_paths[hardware_id] = set(requested_paths)
        manager._desired_grabs[hardware_id] = manager._desired_grab_config_cls(
            paths=set(requested_paths),
            button_map=dict(button_map),
            button_codes=dict(resolved_button_codes),
            force_grab_unmapped=bool(force_grab_unmapped),
        )
    log.info(
        "Grab request for %s: paths=%d mapped_evdev_names=%d mapped_codes=%d",
        hardware_id,
        len(requested_paths),
        len(mapped_evdev_names),
        len(mapped_codes),
    )

    existing_by_path = {
        device.path: device for device in manager.grabbed_devices.get(hardware_id, [])
    }
    for device in existing_by_path.values():
        device.update_button_map(button_map, resolved_button_codes)

    devices = list(existing_by_path.values())
    grabbed_count = 0
    skipped_count = 0
    available_count = 0
    created_global_uinputs = False

    for path in existing_by_path:
        if path in requested_paths:
            manager._cancel_pending_interface_release(hardware_id, path)

    for path in sorted(existing_by_path.keys() - requested_paths):
        manager._schedule_interface_release(hardware_id, path)

    for path in sorted(requested_paths):
        if path in existing_by_path:
            continue
        try:
            raw_device = manager._device_input(path)
            available_count += 1
            caps = raw_device.capabilities()
            has_mapped_buttons = manager._device_has_mapped_buttons(
                caps,
                mapped_evdev_names,
                mapped_codes,
            )

            if has_mapped_buttons or force_grab_unmapped:
                if hardware_id not in manager.grabbed_devices and not created_global_uinputs:
                    manager._create_global_uinputs()
                    created_global_uinputs = True
                detected_types = manager._detect_device_types(raw_device)
                detected_type = primary_input_class_fn(detected_types)

                def mapping_getter(hid: str = hardware_id) -> dict[str, Any]:
                    return manager.active_mappings.get(hid, {})

                device = grabbed_device_cls(
                    path=path,
                    hardware_id=hardware_id,
                    button_map=button_map,
                    button_codes=resolved_button_codes,
                    mapping_getter=mapping_getter,
                    event_callback=manager._on_device_event,
                    device_type=detected_type,
                    device_types=detected_types,
                    verbosity=manager.verbosity,
                    keyboard_uinput=manager._keyboard_uinput,
                    mouse_uinput=manager._mouse_uinput,
                    gamepad_uinput=manager._gamepad_uinput,
                    broadcast_callback=manager.broadcast_callback,
                    recording_manager=manager.recording_manager,
                    macro_player=manager.play_macro,
                    suppress_rel_getter=lambda: manager._mouse_rel_suppressed,
                    mouse_rel_suppression_start_callback=manager.begin_mouse_rel_suppression,
                    diagnostics_recorder=manager._record_diagnostic,
                    runtime_cleanup_callback=manager._clear_combo_runtime_for_binding_scope,
                )
                await manager._grab_with_retry(device, path)
                devices.append(device)
                grabbed_count += 1
                if manager.verbosity >= 1:
                    reason = "mapped buttons" if has_mapped_buttons else "forced for combos"
                    log.debug("  %s - grabbed (%s)", path, reason)
            else:
                skipped_count += 1
                if manager.verbosity >= 1:
                    log.debug("  %s - skipped (no matching mapped button names/codes)", path)
                if manager.verbosity >= 1:
                    log.debug("  %s - skipped (no mapped buttons)", path)
        except OSError as exc:
            if exc.errno in {errno_mod.ENOENT, errno_mod.ENODEV}:
                log.info("Skipping unavailable interface for %s: %s", hardware_id, path)
                continue
            log.error("Failed to grab %s: %s", path, exc)
            for device in devices:
                if device.path in existing_by_path:
                    continue
                await device.release()
            if created_global_uinputs:
                manager._destroy_global_uinputs()
            raise
        except Exception as exc:
            log.error("Failed to grab %s: %s", path, exc)
            for device in devices:
                if device.path in existing_by_path:
                    continue
                await device.release()
            if created_global_uinputs:
                manager._destroy_global_uinputs()
            raise

    waiting_for_device = bool(requested_paths and available_count == 0 and not devices)
    if (
        not waiting_for_device
        and hardware_id not in manager.grabbed_devices
        and requested_paths
        and (mapped_evdev_names or mapped_codes)
        and grabbed_count == 0
    ):
        if created_global_uinputs:
            manager._destroy_global_uinputs()
        raise ValueError(
            f"No interfaces for {hardware_id} matched mapped buttons "
            f"(paths={len(requested_paths)}, mapped_names={len(mapped_evdev_names)}, "
            f"mapped_codes={len(mapped_codes)})"
        )

    if devices:
        manager.grabbed_devices[hardware_id] = devices
    else:
        manager.grabbed_devices.pop(hardware_id, None)

    log.info(
        "Configured device %s: total_interfaces=%d newly_grabbed=%d skipped=%d",
        hardware_id,
        len(devices),
        grabbed_count,
        skipped_count,
    )
    return {
        "grabbed": True,
        "hardware_id": hardware_id,
        "grabbed_count": len(devices),
        "skipped_count": skipped_count,
        "waiting_for_device": waiting_for_device,
    }


async def grab_with_retry(
    manager: Any, device: Any, path: str, *, asyncio_mod: Any, log: Any, errno_mod: Any
) -> None:
    delays = [0.05, 0.10, 0.20, 0.40, 0.80]
    last_error: Exception | None = None
    for attempt, delay in enumerate(delays, start=1):
        try:
            await device.grab()
            return
        except OSError as exc:
            last_error = exc
            if exc.errno != errno_mod.EBUSY:
                raise
            if attempt >= len(delays):
                break
            log.warning(
                "Device %s busy during grab (attempt %d/%d), retrying in %.2fs",
                path,
                attempt,
                len(delays),
                delay,
            )
            await asyncio_mod.sleep(delay)
        except Exception as exc:
            last_error = exc
            raise

    if last_error is not None:
        raise last_error


def device_has_mapped_buttons(
    caps: dict[int, Sequence[object]],
    mapped_evdev_names: set[str],
    mapped_codes: set[int] | None,
    *,
    evdev_mod: Any,
) -> bool:
    mapped_code_set = {int(code) for code in (mapped_codes or set())}
    for ev_type, codes in caps.items():
        if ev_type == evdev_mod.ecodes.EV_SYN:
            continue

        for code in codes:
            if isinstance(code, tuple):
                if not code or not isinstance(code[0], int):
                    continue
                code_val = code[0]
            elif isinstance(code, int):
                code_val = code
            else:
                continue

            if code_val in mapped_code_set:
                return True

            try:
                code_name = evdev_mod.ecodes.bytype[ev_type].get(code_val, str(code_val))
                if isinstance(code_name, (tuple, list)):
                    code_name = code_name[0] if code_name else str(code_val)
                if code_name.lower() in mapped_evdev_names:
                    return True
            except Exception:
                pass

    return False


async def release_device_unlocked(manager: Any, hardware_id: str, *, log: Any) -> dict[str, object]:
    manager._cancel_pending_hardware_release(hardware_id)
    manager._cancel_pending_interface_releases_for_hardware(hardware_id)
    await manager._clear_combo_runtime_for_binding_scope(hardware_id)
    manager._desired_grabs.pop(hardware_id, None)
    devices = manager.grabbed_devices.pop(hardware_id, [])

    for device in devices:
        await device.release()

    manager._destroy_global_uinputs()
    manager.active_mappings.pop(hardware_id, None)
    manager._desired_paths.pop(hardware_id, None)
    log.info("Released device %s", hardware_id)
    return {"released": True, "hardware_id": hardware_id}


def schedule_hardware_release_unlocked(
    manager: Any, hardware_id: str, grace_s: float | None, *, asyncio_mod: Any, log: Any
) -> dict[str, object]:
    devices = manager.grabbed_devices.get(hardware_id, [])
    if not devices:
        manager._desired_grabs.pop(hardware_id, None)
        manager.active_mappings.pop(hardware_id, None)
        manager._desired_paths.pop(hardware_id, None)
        return {"released": True, "hardware_id": hardware_id}

    manager.active_mappings[hardware_id] = {}
    manager._desired_paths[hardware_id] = set()

    delay = max(0.01, float(manager._release_grace_s if grace_s is None else grace_s))
    manager._cancel_pending_hardware_release(hardware_id)
    manager._pending_hardware_release[hardware_id] = asyncio_mod.create_task(
        manager._delayed_hardware_release(hardware_id, delay)
    )
    log.info("Scheduled hardware release for %s in %.1fs", hardware_id, delay)
    return {
        "released": False,
        "scheduled": True,
        "hardware_id": hardware_id,
        "grace_s": delay,
    }


async def delayed_hardware_release(
    manager: Any, hardware_id: str, delay: float, *, asyncio_mod: Any, log: Any
) -> None:
    next_delay = float(delay)
    try:
        while True:
            await asyncio_mod.sleep(next_delay)
            async with manager._op_lock:
                task = manager._pending_hardware_release.get(hardware_id)
                if task is not asyncio_mod.current_task():
                    return
                if manager._desired_paths.get(hardware_id):
                    return
                if manager._hardware_has_held_inputs(hardware_id):
                    next_delay = manager._held_release_retry_s
                    log.info(
                        "Deferred release for %s: source button still held, retrying in %.1fs",
                        hardware_id,
                        next_delay,
                    )
                    continue
                await manager._release_device_unlocked(hardware_id)
                return
    except asyncio_mod.CancelledError:
        pass
    finally:
        task = manager._pending_hardware_release.get(hardware_id)
        if task is asyncio_mod.current_task():
            manager._pending_hardware_release.pop(hardware_id, None)


def hardware_has_held_inputs(manager: Any, hardware_id: str) -> bool:
    for device in manager.grabbed_devices.get(hardware_id, []):
        if device.has_held_source_inputs():
            return True
    return False


def cancel_pending_hardware_release(manager: Any, hardware_id: str) -> None:
    task = manager._pending_hardware_release.pop(hardware_id, None)
    if task and not task.done():
        task.cancel()


def cancel_pending_interface_release(manager: Any, hardware_id: str, path: str) -> None:
    key = (hardware_id, path)
    task = manager._pending_interface_release.pop(key, None)
    if task and not task.done():
        task.cancel()


def cancel_pending_interface_releases_for_hardware(manager: Any, hardware_id: str) -> None:
    for key in list(manager._pending_interface_release.keys()):
        if key[0] != hardware_id:
            continue
        task = manager._pending_interface_release.pop(key)
        if not task.done():
            task.cancel()


def schedule_interface_release(
    manager: Any, hardware_id: str, path: str, *, asyncio_mod: Any, log: Any
) -> None:
    manager._cancel_pending_interface_release(hardware_id, path)
    delay = manager._release_grace_s
    manager._pending_interface_release[(hardware_id, path)] = asyncio_mod.create_task(
        manager._delayed_interface_release(hardware_id, path, delay)
    )
    log.info("Scheduled interface release for %s (%s) in %.1fs", hardware_id, path, delay)


async def delayed_interface_release(
    manager: Any, hardware_id: str, path: str, delay: float, *, asyncio_mod: Any
) -> None:
    key = (hardware_id, path)
    try:
        await asyncio_mod.sleep(delay)
        async with manager._op_lock:
            task = manager._pending_interface_release.get(key)
            if task is not asyncio_mod.current_task():
                return
            if path in manager._desired_paths.get(hardware_id, set()):
                return
            await manager._release_interface_unlocked(hardware_id, path)
    except asyncio_mod.CancelledError:
        pass
    finally:
        task = manager._pending_interface_release.get(key)
        if task is asyncio_mod.current_task():
            manager._pending_interface_release.pop(key, None)


async def release_interface_unlocked(manager: Any, hardware_id: str, path: str) -> None:
    devices = manager.grabbed_devices.get(hardware_id, [])
    keep: list[Any] = []
    removed: Any = None
    for device in devices:
        if removed is None and device.path == path:
            removed = device
            continue
        keep.append(device)

    if removed is None:
        return

    await manager._clear_combo_runtime_for_binding_scope(
        hardware_id,
        str(getattr(removed, "interface_id", "") or "").lower(),
    )
    removed.release_tracked_outputs()
    await removed.release()

    if keep:
        manager.grabbed_devices[hardware_id] = keep
    else:
        manager.grabbed_devices.pop(hardware_id, None)
        if not manager._desired_paths.get(hardware_id):
            manager.active_mappings.pop(hardware_id, None)
            manager._desired_paths.pop(hardware_id, None)
            manager._desired_grabs.pop(hardware_id, None)
        manager._destroy_global_uinputs()


async def release_all_devices(manager: Any) -> None:
    async with manager._op_lock:
        await manager.cancel_macro_playback()
        await manager._clear_combo_runtime()
        hardware_ids = set(manager.grabbed_devices) | set(manager._desired_grabs)
        for hardware_id in list(hardware_ids):
            await manager._release_device_unlocked(hardware_id)


async def set_mapping(
    manager: Any, hardware_id: str, mapping: dict[str, object], *, json_object_fn: Any, log: Any
) -> dict[str, object]:
    async with manager._op_lock:
        manager._cancel_pending_hardware_release(hardware_id)
        if hardware_id not in manager.grabbed_devices:
            raise ValueError(f"Device {hardware_id} not grabbed")

        parsed_mapping: dict[str, Any] = {}
        for button_id, action_data in mapping.items():
            action_dict = json_object_fn(action_data)
            if isinstance(action_data, str):
                parsed_mapping[button_id] = manager._parse_action(action_data)
            elif action_dict is not None:
                parsed_mapping[button_id] = manager._parse_action(action_dict)

        manager.active_mappings[hardware_id] = parsed_mapping
        for device in manager.grabbed_devices.get(hardware_id, []):
            await device.reset_mapping_runtime_state()
        log.info("Updated mapping for %s (%d buttons)", hardware_id, len(parsed_mapping))
        return {"updated": True, "hardware_id": hardware_id}
