"""Health data from the nightly iPhone Shortcuts export.

Reads $IPHONE_EXPORT_PATH/YYYY-MM-DD.json (one file per logical day,
written to an iCloud-synced folder by a Shortcuts automation) and emits
chunks in the same text formats the backup-based source produced.
"""

import json
import logging
import os
from datetime import datetime

from .base import Chunk, fmt_duration

log = logging.getLogger(__name__)


class IPhoneExportSource:
    def __init__(self, export_dir: str, local_tz):
        self._export_dir = export_dir
        self._local_tz = local_tz

    def get_chunks(self, start: datetime, end: datetime) -> list[Chunk]:
        date_str = start.strftime("%Y-%m-%d")
        path = os.path.join(self._export_dir, f"{date_str}.json")
        if not os.path.exists(path):
            log.info(f"  iphone_export: no file for {date_str}")
            return []

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"  iphone_export: unreadable {path}: {e}")
            return []

        if data.get("date") not in (None, date_str):
            log.warning(f"  iphone_export: {path} says date={data['date']}, trusting filename")

        chunks = []
        if "sleep" in data:
            chunks.extend(self._sleep_chunks(data["sleep"]))
        if "vitals" in data:
            chunks.extend(self._vitals_chunks(data["vitals"], start))
        if "activity" in data:
            chunks.extend(self._activity_chunks(data["activity"]))
        if "workouts" in data:
            chunks.extend(self._workout_chunks(data["workouts"]))
        chunks.sort(key=lambda c: c.window_start)
        log.info(f"  iphone_export: {len(chunks)} chunks")
        return chunks

    def _ts(self, iso: str) -> datetime:
        return datetime.fromisoformat(iso).astimezone(self._local_tz)

    def _sleep_chunks(self, sleep: dict) -> list[Chunk]:
        core_s = sleep.get("core_min", 0) * 60
        deep_s = sleep.get("deep_min", 0) * 60
        rem_s = sleep.get("rem_min", 0) * 60
        awake_s = sleep.get("awake_min", 0) * 60
        total_s = core_s + deep_s + rem_s  # exclude awake from "sleep"
        if not total_s:
            return []

        ts = self._ts(sleep["start"])
        parts = []
        if core_s:  parts.append(f"Core {fmt_duration(core_s)}")
        if deep_s:  parts.append(f"Deep {fmt_duration(deep_s)}")
        if rem_s:   parts.append(f"REM {fmt_duration(rem_s)}")
        if awake_s: parts.append(f"awake {fmt_duration(awake_s)}")
        text = (
            f"[{ts.strftime('%Y-%m-%d %H:%M')}] "
            f"Sleep: {fmt_duration(total_s)} total — {', '.join(parts)}."
        )
        if sleep.get("resp_rate_avg"):
            text += f" Avg respiratory rate: {sleep['resp_rate_avg']:.1f} breaths/min."

        return [Chunk(
            window_start=ts.isoformat(),
            text=text,
            apps=[], total_secs=int(total_s), source="iphone_export",
            metadata={"kind": "sleep"},
        )]

    def _vitals_chunks(self, vitals: dict, window_start: datetime) -> list[Chunk]:
        parts = []
        if vitals.get("resting_hr"):
            parts.append(f"resting HR {round(vitals['resting_hr'])}bpm")
        if vitals.get("hrv_ms"):
            parts.append(f"HRV {round(vitals['hrv_ms'])}ms")
        if vitals.get("walking_hr_avg"):
            parts.append(f"walking HR avg {round(vitals['walking_hr_avg'])}bpm")
        if vitals.get("vo2_max"):
            parts.append(f"VO2 max {vitals['vo2_max']:.1f} ml/kg/min")
        if vitals.get("exercise_min"):
            parts.append(f"{round(vitals['exercise_min'])} exercise min")
        if vitals.get("daylight_min"):
            parts.append(f"{round(vitals['daylight_min'])} min daylight")
        if not parts:
            return []

        ts = self._ts(vitals["time"]) if vitals.get("time") else window_start
        return [Chunk(
            window_start=ts.isoformat(),
            text=f"[{ts.strftime('%Y-%m-%d %H:%M')}] Daily vitals: {', '.join(parts)}.",
            apps=[], total_secs=0, source="iphone_export",
            metadata={"kind": "vitals"},
        )]

    def _activity_chunks(self, entries: list) -> list[Chunk]:
        chunks = []
        for entry in entries:
            ts = self._ts(entry["hour"])
            parts = []
            if entry.get("steps"):
                parts.append(f"{int(entry['steps'])} steps")
            if entry.get("avg_hr"):
                parts.append(f"avg HR {round(entry['avg_hr'])}bpm")
            if entry.get("avg_mets"):
                parts.append(f"avg effort {entry['avg_mets']:.1f} METs")
            if not parts:
                continue
            chunks.append(Chunk(
                window_start=ts.isoformat(),
                text=f"[{ts.strftime('%Y-%m-%d %H:%M')}] Activity: {', '.join(parts)}.",
                apps=[], total_secs=3600, source="iphone_export",
            ))
        return chunks

    def _workout_chunks(self, entries: list) -> list[Chunk]:
        chunks = []
        for entry in entries:
            ts = self._ts(entry["start"])
            duration_s = entry.get("duration_min", 0) * 60
            parts = [fmt_duration(duration_s)]
            if entry.get("avg_hr"):
                parts.append(f"avg HR {round(entry['avg_hr'])}bpm")
            if entry.get("kcal"):
                parts.append(f"{round(entry['kcal'])} kcal")
            if entry.get("distance_km"):
                parts.append(f"{entry['distance_km']:.2f}km")
            chunks.append(Chunk(
                window_start=ts.isoformat(),
                text=f"[{ts.strftime('%Y-%m-%d %H:%M')}] {entry['type']}: {', '.join(parts)}.",
                apps=[], total_secs=int(duration_s), source="iphone_export",
                metadata={"kind": "workout"},
            ))
        return chunks
