#!/usr/bin/env python3
"""Capture the complete AppImage icon gallery rendered through Brotway."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from browser_probe import image_metrics, page_state, performance_summary, ready_surface, wait_until
from selenium import webdriver
from selenium.webdriver.chrome.service import Service

URL = os.environ.get("KEYMASQ_BROTWAY_URL", "http://deck:18102/")
OUTPUT_DIR = Path(os.environ.get("KEYMASQ_BROTWAY_OUTPUT_DIR", "/tmp"))
TIMEOUT = int(os.environ.get("KEYMASQ_BROTWAY_BROWSER_TIMEOUT", "90"))


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    screenshot_path = OUTPUT_DIR / "icon-gallery.png"
    result_path = OUTPUT_DIR / "icon-gallery-browser-result.json"

    options = webdriver.ChromeOptions()
    options.binary_location = os.environ["KEYMASQ_CHROMIUM"]
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1800,1200")
    options.add_argument("--force-device-scale-factor=1")
    options.set_capability("goog:loggingPrefs", {"browser": "ALL", "performance": "ALL"})

    result: dict[str, Any] = {"url": URL}
    driver = webdriver.Chrome(service=Service(os.environ["KEYMASQ_CHROMEDRIVER"]), options=options)
    try:
        driver.set_page_load_timeout(TIMEOUT)
        driver.get(URL)

        def complete_gallery() -> dict[str, Any] | None:
            state = page_state(driver)
            if not ready_surface(state):
                return None
            visible_surfaces = [
                surface
                for surface in state["surfaces"]
                if surface["protocolVisible"] and surface["cssVisibility"] == "visible"
            ]
            if not visible_surfaces:
                return None
            gallery = max(
                visible_surfaces,
                key=lambda surface: surface["width"] * surface["height"],
            )
            if gallery["width"] < 1400 or gallery["height"] < 700:
                return None
            png = driver.get_screenshot_as_png()
            metrics = image_metrics(png)
            if max(metrics["stddev"]) < 4.0:
                return None
            return {"state": state, "gallery": gallery, "png": png, "metrics": metrics}

        rendered = wait_until(
            "the complete icon gallery surface", complete_gallery, timeout=TIMEOUT
        )
        screenshot_path.write_bytes(rendered["png"])
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
                "browser_log": browser_log,
                "gallery_surface": rendered["gallery"],
                "image": rendered["metrics"],
                "performance": performance,
            }
        )
        result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - preserve all browser failure diagnostics
        result["error"] = f"{type(exc).__name__}: {exc}"
        try:
            result["page"] = page_state(driver)
            result["browser_log"] = driver.get_log("browser")
            result["performance"] = performance_summary(driver.get_log("performance"))
            failure_png = driver.get_screenshot_as_png()
            screenshot_path.write_bytes(failure_png)
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
