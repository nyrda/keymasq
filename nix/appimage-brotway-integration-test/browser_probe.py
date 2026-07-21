#!/usr/bin/env python3
"""Drive the Brotway page with Chromium and prove rendering plus input."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver import ActionChains, Keys
from selenium.webdriver.chrome.service import Service

URL = os.environ.get("KEYMASQ_BROTWAY_URL", "http://deck:18101/")
OUTPUT_DIR = Path(os.environ.get("KEYMASQ_BROTWAY_OUTPUT_DIR", "/tmp"))
TIMEOUT = int(os.environ.get("KEYMASQ_BROTWAY_BROWSER_TIMEOUT", "90"))
HOLD_SECONDS = int(os.environ.get("KEYMASQ_BROTWAY_HOLD_SECONDS", "0"))


def image_metrics(png: bytes) -> dict[str, Any]:
    image = Image.open(BytesIO(png)).convert("RGB")
    stats = ImageStat.Stat(image)
    return {
        "size": list(image.size),
        "sha256": hashlib.sha256(png).hexdigest(),
        "stddev": [round(value, 3) for value in stats.stddev],
        "extrema": [list(channel) for channel in image.getextrema()],
    }


def changed_fraction(before_png: bytes, after_png: bytes) -> float:
    before = Image.open(BytesIO(before_png)).convert("RGB")
    after = Image.open(BytesIO(after_png)).convert("RGB")
    if before.size != after.size:
        return 1.0
    difference = ImageChops.difference(before, after).convert("L")
    changed = sum(count for value, count in enumerate(difference.histogram()) if value >= 8)
    return changed / (before.width * before.height)


def page_state(driver: webdriver.Chrome) -> dict[str, Any]:
    return driver.execute_script(
        """
        const knownSurfaces = typeof surfaces === 'undefined'
          ? []
          : Object.values(surfaces);
        const surfaceState = knownSurfaces.map((surface) => {
          const div = surface.div;
          const rect = div.getBoundingClientRect();
          const descendants = Array.from(div.querySelectorAll('*'));
          const images = Array.from(div.querySelectorAll('img'));
          return {
            id: surface.id,
            protocolVisible: surface.visible,
            cssVisibility: getComputedStyle(div).visibility,
            width: rect.width,
            height: rect.height,
            childElementCount: div.childElementCount,
            descendantCount: descendants.length,
            contentCount: descendants.filter((element) => element.__content).length,
            imageCount: images.length,
            loadedImageCount: images.filter(
              (image) => image.complete && image.naturalWidth > 0 && image.naturalHeight > 0
            ).length,
            imageSources: images.slice(0, 8).map((image) => image.src.slice(0, 96)),
            htmlPrefix: div.outerHTML.slice(0, 800),
          };
        });
        return {
          readyState: document.readyState,
          title: document.title,
          surfaceCount: surfaceState.length,
          visibleSurfaceCount: surfaceState.filter(
            (surface) => surface.protocolVisible && surface.cssVisibility === 'visible'
          ).length,
          surfaces: surfaceState,
          textureIds: typeof textures === 'undefined' ? [] : Object.keys(textures),
          zoomRootChildren: document.querySelector('#zoomRoot')?.childElementCount ?? 0,
          bodyChildren: document.body ? document.body.children.length : 0,
        };
        """
    )


def ready_surface(state: dict[str, Any]) -> bool:
    return any(
        surface["protocolVisible"]
        and surface["cssVisibility"] == "visible"
        and surface["width"] > 0
        and surface["height"] > 0
        and surface["contentCount"] > 0
        for surface in state["surfaces"]
    )


def wait_until(description: str, predicate: Any, timeout: int = TIMEOUT) -> Any:
    deadline = time.monotonic() + timeout
    last_value: Any = None
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            last_value = predicate()
            if last_value:
                return last_value
        except (WebDriverException, KeyError, TypeError) as exc:
            last_error = exc
        time.sleep(0.25)
    detail = f"; last error: {last_error}" if last_error else f"; last value: {last_value!r}"
    raise RuntimeError(f"timed out waiting for {description}{detail}")


def performance_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    websocket_urls: list[str] = []
    websocket_frames = 0
    websocket_frame_samples: list[dict[str, Any]] = []
    methods: dict[str, int] = {}
    for entry in entries:
        try:
            message = json.loads(entry["message"])["message"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        method = message.get("method", "")
        methods[method] = methods.get(method, 0) + 1
        if method == "Network.webSocketCreated":
            websocket_urls.append(message.get("params", {}).get("url", ""))
        elif method == "Network.webSocketFrameReceived":
            websocket_frames += 1
            if len(websocket_frame_samples) < 12:
                response = message.get("params", {}).get("response", {})
                payload = response.get("payloadData", "")
                opcode = response.get("opcode")
                try:
                    raw = base64.b64decode(payload) if opcode == 2 else payload.encode()
                    prefix = raw[:24].hex()
                    decoded_length = len(raw)
                except (ValueError, TypeError):
                    prefix = "<decode failed>"
                    decoded_length = -1
                websocket_frame_samples.append(
                    {
                        "opcode": opcode,
                        "encoded_length": len(payload),
                        "decoded_length": decoded_length,
                        "prefix_hex": prefix,
                    }
                )
    return {
        "websocket_urls": websocket_urls,
        "websocket_frames_received": websocket_frames,
        "websocket_frame_samples": websocket_frame_samples,
        "selected_event_counts": {
            method: count
            for method, count in methods.items()
            if method.startswith("Network.webSocket")
        },
    }


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    before_path = OUTPUT_DIR / "brotway-before.png"
    after_path = OUTPUT_DIR / "brotway-after-triple-shift.png"
    result_path = OUTPUT_DIR / "brotway-browser-result.json"

    options = webdriver.ChromeOptions()
    options.binary_location = os.environ["KEYMASQ_CHROMIUM"]
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--force-device-scale-factor=1")
    options.set_capability("goog:loggingPrefs", {"browser": "ALL", "performance": "ALL"})

    result: dict[str, Any] = {"url": URL}
    driver = webdriver.Chrome(service=Service(os.environ["KEYMASQ_CHROMEDRIVER"]), options=options)
    try:
        driver.set_page_load_timeout(TIMEOUT)
        driver.get(URL)

        def visible_render() -> dict[str, Any] | None:
            state = page_state(driver)
            png = driver.get_screenshot_as_png()
            metrics = image_metrics(png)
            if ready_surface(state) and max(metrics["stddev"]) >= 4.0:
                return {"state": state, "png": png, "metrics": metrics}
            return None

        wait_until("a visibly rendered Brotway surface", visible_render)
        surface_elements = [
            surface
            for surface in driver.find_elements("css selector", "#zoomRoot > div")
            if surface.is_displayed() and surface.size["width"] > 0 and surface.size["height"] > 0
        ]
        surface_element = max(
            surface_elements,
            key=lambda element: element.size["width"] * element.size["height"],
        )
        ActionChains(driver).move_to_element(surface_element).click().perform()
        time.sleep(0.5)
        state_before = page_state(driver)
        before_png = driver.get_screenshot_as_png()
        before_path.write_bytes(before_png)
        before_metrics = image_metrics(before_png)
        if max(before_metrics["stddev"]) < 4.0:
            raise RuntimeError(f"Brotway screenshot appears blank: {before_metrics}")

        actions = ActionChains(driver)
        for _ in range(3):
            actions.key_down(Keys.SHIFT).key_up(Keys.SHIFT).pause(0.12)
        actions.perform()

        def debug_rendered() -> dict[str, Any] | None:
            state = page_state(driver)
            png = driver.get_screenshot_as_png()
            fraction = changed_fraction(before_png, png)
            if state["surfaceCount"] > state_before["surfaceCount"] or fraction >= 0.005:
                return {"state": state, "png": png, "changed_fraction": fraction}
            return None

        debug = wait_until("the triple-Shift debug-menu render", debug_rendered, timeout=30)
        after_png = debug["png"]
        after_path.write_bytes(after_png)
        time.sleep(1.0)
        performance = performance_summary(driver.get_log("performance"))
        browser_log = driver.get_log("browser")
        severe = [
            entry
            for entry in browser_log
            if entry.get("level") == "SEVERE" and "favicon.ico" not in entry.get("message", "")
        ]

        if not performance["websocket_urls"]:
            raise RuntimeError(f"Chromium observed no Brotway WebSocket: {performance}")
        if performance["websocket_frames_received"] < 1:
            raise RuntimeError(f"Chromium received no Brotway WebSocket frames: {performance}")
        if severe:
            raise RuntimeError(f"severe Chromium console messages: {severe}")

        result.update(
            {
                "before": state_before,
                "after": debug["state"],
                "before_image": before_metrics,
                "after_image": image_metrics(after_png),
                "changed_fraction": round(debug["changed_fraction"], 6),
                "performance": performance,
                "browser_log": browser_log,
                "input": "triple-Shift",
            }
        )
        result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(json.dumps(result, indent=2, sort_keys=True))
        if HOLD_SECONDS > 0:
            time.sleep(HOLD_SECONDS)
        return 0
    except Exception as exc:  # noqa: BLE001 - preserve all browser failure diagnostics
        result["error"] = f"{type(exc).__name__}: {exc}"
        try:
            result["page"] = page_state(driver)
            result["browser_log"] = driver.get_log("browser")
            result["performance"] = performance_summary(driver.get_log("performance"))
            failure_png = driver.get_screenshot_as_png()
            after_path.write_bytes(failure_png)
            result["failure_image"] = image_metrics(failure_png)
        except Exception as diagnostic_exc:  # noqa: BLE001 - best-effort diagnostics
            result["diagnostic_error"] = f"{type(diagnostic_exc).__name__}: {diagnostic_exc}"
        result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(json.dumps(result, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    finally:
        driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
