"""Generates the synthetic fixture catalog and query set.

SYNTHETIC ONLY. This script and everything it writes are deleted once the official
starter assets arrive. See README.md in this directory.

    python data/fixtures_synthetic/generate_fixtures.py
"""

from __future__ import annotations

import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent

WARNING = "SYNTHETIC FIXTURE. Not official data. Delete when the real dataset arrives."


def dl(num: int, desc: str, msg: str, qna: str, kind: str = "screen") -> dict:
    return {
        "deeplink": f"bixby://masked/act/{num}",
        "description": desc,
        "message": msg,
        "qna_description": qna,
        "originalType": kind,
    }


DEEPLINKS = [
    # --- Battery ---
    dl(9001, "Open battery settings under Device care", "View remaining battery and usage",
       "Shows current battery level and estimated time remaining"),
    dl(9002, "Open battery usage by app", "See which apps drain the most power",
       "Lists per-app battery consumption over recent days"),
    dl(9003, "Toggle power saving mode", "Reduce background activity to extend battery",
       "Enables power saving, limiting background data and performance", "toggle"),
    dl(9004, "Open background usage limits", "Restrict apps running in the background",
       "Controls deep sleeping, sleeping and never-sleeping app lists"),
    dl(9005, "Toggle adaptive battery", "Learn usage patterns to save power",
       "Limits battery for apps you rarely use", "toggle"),
    dl(9006, "Open charging settings", "Configure fast and protective charging",
       "Controls fast charging, super fast charging and battery protection"),
    dl(9007, "Toggle fast charging", "Charge the device more quickly",
       "Enables fast wired charging when a compatible charger is attached", "toggle"),
    dl(9008, "Open wireless power sharing", "Share battery with another device",
       "Allows charging accessories from the battery of this device"),
    dl(9009, "Open battery protection settings", "Limit maximum charge to extend lifespan",
       "Caps charging at a reduced percentage to preserve battery health"),
    dl(9010, "Open device care diagnostics", "Run a full device health check",
       "Scans battery, storage and memory and reports problems"),
    # --- Display ---
    dl(9101, "Open display settings", "Adjust screen appearance and behaviour",
       "Top level screen for brightness, mode and resolution"),
    dl(9102, "Open brightness settings", "Change how bright the screen is",
       "Controls manual brightness and adaptive brightness behaviour"),
    dl(9103, "Toggle adaptive brightness", "Adjust brightness to your surroundings",
       "Automatically tunes brightness based on ambient light", "toggle"),
    dl(9104, "Open screen timeout settings", "Set how long before the screen sleeps",
       "Controls the idle delay before the display turns off"),
    dl(9105, "Open navigation bar settings under Display", "Choose navigation type",
       "Switches between button navigation and swipe gestures"),
    dl(9106, "Open screen resolution settings", "Change display sharpness",
       "Selects between HD, FHD and QHD screen resolution"),
    dl(9107, "Open motion smoothness settings", "Change the screen refresh rate",
       "Switches between standard and adaptive refresh rate"),
    dl(9108, "Toggle blue light filter", "Reduce eye strain at night",
       "Applies a warm colour filter to the display", "toggle"),
    dl(9109, "Open screen mode and colour settings", "Adjust colour vividness",
       "Switches between vivid and natural screen colour profiles"),
    dl(9110, "Open always on display settings", "Show information on a sleeping screen",
       "Controls when the always on display appears"),
    # --- Camera ---
    dl(9201, "Open camera settings", "Configure how the camera behaves",
       "Top level screen for camera capture options"),
    dl(9202, "Open rear camera resolution settings", "Change photo size and quality",
       "Selects megapixel count and aspect ratio for the rear camera"),
    dl(9203, "Open video resolution settings", "Change recording quality",
       "Selects video resolution and frame rate for recording"),
    dl(9204, "Toggle scene optimiser", "Improve photos automatically",
       "Detects the scene and adjusts capture settings", "toggle"),
    dl(9205, "Open camera grid and guide settings", "Show composition guides",
       "Displays gridlines to help frame a photo"),
    dl(9206, "Toggle video stabilisation", "Reduce blur while recording",
       "Applies stabilisation during video capture", "toggle"),
    dl(9207, "Open camera storage location settings", "Choose where photos are saved",
       "Selects internal storage or memory card for captures"),
    dl(9208, "Reset camera settings to default", "Restore original camera configuration",
       "Returns all camera options to factory defaults"),
    dl(9209, "Open camera permissions", "Control which apps use the camera",
       "Manages per-app access to the camera hardware"),
    dl(9210, "Clean the camera lens", "Wipe the lens to remove smudges",
       "Physical guidance for removing smudges from the lens", "manual"),
    # --- Performance ---
    dl(9301, "Open device care memory settings", "Free up memory used by apps",
       "Shows memory usage and lets you close background apps"),
    dl(9302, "Open storage settings", "Free up space on the device",
       "Shows storage usage and removable files"),
    dl(9303, "Open app info and cache settings", "Clear cached data for an app",
       "Per-app storage, cache and data controls"),
    dl(9304, "Toggle processing speed mode", "Prioritise performance over battery",
       "Switches between optimised and high performance profiles", "toggle"),
    dl(9305, "Open software update settings", "Install the latest system update",
       "Checks for and installs firmware updates"),
    dl(9306, "Open running services", "See what is running in the background",
       "Lists active background services and their memory use"),
    dl(9307, "Open animation scale settings", "Speed up interface animations",
       "Adjusts window, transition and animator duration scales"),
    dl(9308, "Restart the device", "Restart to clear temporary problems",
       "Performs a normal restart of the device", "critical"),
    dl(9309, "Restart in safe mode", "Start with third party apps disabled",
       "Boots the device with only preinstalled apps active", "critical"),
    dl(9310, "Reset all settings", "Return all settings to default",
       "Resets system settings without deleting personal files", "critical"),
]

QUERIES = [
    {"domain": "Battery", "query": "My battery is draining very quickly"},
    {"domain": "Battery", "query": "Phone takes forever to charge"},
    {"domain": "Battery", "query": "Battery percentage drops suddenly overnight"},
    {"domain": "Battery", "query": "How do I make my battery last longer"},
    {"domain": "Display", "query": "Screen is too dim outdoors"},
    {"domain": "Display", "query": "Swipe gestures go the wrong way after installing an app"},
    {"domain": "Display", "query": "Screen turns off too fast"},
    {"domain": "Display", "query": "Colours look washed out on my screen"},
    {"domain": "Camera", "query": "My photos look blurry"},
    {"domain": "Camera", "query": "How do I change the camera resolution"},
    {"domain": "Camera", "query": "Videos come out shaky when I record"},
    {"domain": "Camera", "query": "Camera app will not open"},
    {"domain": "Performance", "query": "My phone got slow after the update"},
    {"domain": "Performance", "query": "Apps keep closing on their own"},
    {"domain": "Performance", "query": "Device feels laggy when switching apps"},
    {"domain": "Performance", "query": "Running out of storage space"},
]

# Held-out paraphrases used to tune the cache similarity threshold. Deliberately kept
# separate from QUERIES so that threshold tuning is never measured on the indexed text.
PARAPHRASES = [
    ("My battery is draining very quickly", "My phone battery dies really fast"),
    ("Phone takes forever to charge", "Charging is super slow on my device"),
    ("Screen is too dim outdoors", "I cannot see my display in sunlight"),
    ("My photos look blurry", "Pictures are coming out fuzzy"),
    ("My phone got slow after the update", "Device became sluggish since the latest update"),
    ("Running out of storage space", "There is no space left on my phone"),
]


def main() -> None:
    (HERE / "deeplinks.json").write_text(
        json.dumps(
            {"_synthetic": True, "_warning": WARNING, "deeplinks": DEEPLINKS}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    (HERE / "queries.json").write_text(
        json.dumps(
            {
                "_synthetic": True,
                "_warning": WARNING,
                "queries": QUERIES,
                "paraphrase_pairs": [
                    {"original": a, "paraphrase": b} for a, b in PARAPHRASES
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"deeplinks: {len(DEEPLINKS)}  queries: {len(QUERIES)}  "
          f"paraphrase pairs: {len(PARAPHRASES)}")


if __name__ == "__main__":
    main()
