"""Health data from the nightly iPhone Shortcuts export.

Reads $IPHONE_EXPORT_PATH/YYYY-MM-DD.json — one file per logical day,
POSTed by a Shortcuts automation and stored by the receiver service —
and emits chunks in the text formats the diary and query pipeline expect.
"""

import json
import logging
import os
import re
from datetime import datetime

from .base import Chunk, fmt_duration

log = logging.getLogger(__name__)

# Shortcuts' default date rendering, e.g. "Jul 9, 2026 at 2:41 AM"
_LOCALE_DT_RE = re.compile(r"[A-Z][a-z]{2} \d{1,2}, \d{4} at \d{1,2}:\d{2} [AP]M")
_LOCALE_DT_FMT = "%b %d, %Y at %I:%M %p"


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
            chunks.extend(self._sleep_samples_chunks(data["sleep"]))
        if "vitals" in data:
            chunks.extend(self._vitals_chunks(data["vitals"], start))
        elif "resting_hr" in data or "hrv" in data:
            chunks.extend(self._vitals_from_lines(data.get("resting_hr"), data.get("hrv"), start))
        if "activity" in data:
            chunks.extend(self._activity_chunks(data["activity"]))
        elif "step" in data or "steps" in data:
            chunks.extend(self._activity_from_lines(data.get("step", data.get("steps"))))
        chunks.sort(key=lambda c: c.window_start)
        log.info(f"  iphone_export: {len(chunks)} chunks")
        return chunks

    def _ts(self, iso: str) -> datetime:
        return datetime.fromisoformat(iso).astimezone(self._local_tz)

    def _sleep_samples_chunks(self, raw) -> list[Chunk]:
        """Aggregate raw per-sample lines ("startISO,endISO,stage") into one chunk.

        Accepts one newline-joined string or a JSON array of line strings —
        Shortcuts produces either depending on how the repeat results are bound.
        """
        if isinstance(raw, list):
            raw = "\n".join(str(item) for item in raw)
        stage_secs: dict[str, float] = {}
        earliest = None
        for line in raw.strip().splitlines():
            parsed = self._parse_sample_line(line)
            if parsed is None:
                log.warning(f"  iphone_export: bad sleep sample line: {line!r}")
                continue
            start, end, stage = parsed
            key = stage.strip().lower()
            if key not in ("core", "deep", "rem", "awake"):
                log.warning(f"  iphone_export: unknown sleep stage: {stage!r}")
                continue
            stage_secs[key] = stage_secs.get(key, 0) + (end - start).total_seconds()
            if earliest is None or start < earliest:
                earliest = start
        if earliest is None:
            return []
        return self._build_sleep_chunk(earliest.astimezone(self._local_tz), stage_secs)

    def _parse_sample_line(self, line: str) -> tuple[datetime, datetime, str] | None:
        """Parse one sample line into (start, end, stage).

        Handles Shortcuts' default locale rendering with the values jammed
        together ("Jul 9, 2026 at 2:41 AMJul 9, 2026 at 2:53 AMCore" —
        no separators, no offsets, so times are taken as local), and the
        ISO contract form "startISO,endISO,stage" (comma- or space-separated).
        """
        line = line.replace(" ", " ").strip()  # narrow no-break space before AM/PM

        matches = list(_LOCALE_DT_RE.finditer(line))
        if len(matches) >= 2:
            start = datetime.strptime(matches[0].group(), _LOCALE_DT_FMT).replace(tzinfo=self._local_tz)
            end = datetime.strptime(matches[1].group(), _LOCALE_DT_FMT).replace(tzinfo=self._local_tz)
            return start, end, line[matches[1].end():].strip(" ,")

        parts = line.split(",") if "," in line else line.split(None, 2)
        try:
            start_s, end_s, stage = parts
            return datetime.fromisoformat(start_s.strip()), datetime.fromisoformat(end_s.strip()), stage
        except ValueError:
            return None

    def _build_sleep_chunk(self, ts: datetime, stage_secs: dict) -> list[Chunk]:
        core_s = stage_secs.get("core", 0)
        deep_s = stage_secs.get("deep", 0)
        rem_s = stage_secs.get("rem", 0)
        awake_s = stage_secs.get("awake", 0)
        total_s = core_s + deep_s + rem_s  # exclude awake from "sleep"
        if not total_s:
            return []

        parts = []
        if core_s:  parts.append(f"Core {fmt_duration(core_s)}")
        if deep_s:  parts.append(f"Deep {fmt_duration(deep_s)}")
        if rem_s:   parts.append(f"REM {fmt_duration(rem_s)}")
        if awake_s: parts.append(f"awake {fmt_duration(awake_s)}")
        text = (
            f"[{ts.strftime('%Y-%m-%d %H:%M')}] "
            f"Sleep: {fmt_duration(total_s)} total — {', '.join(parts)}."
        )
        return [Chunk(
            window_start=ts.isoformat(),
            text=text,
            apps=[], total_secs=int(total_s), source="iphone_export",
            metadata={"kind": "sleep"},
        )]

    def _quantity_lines(self, raw) -> list[tuple[datetime, datetime, float]]:
        """Parse sample lines whose third part is a number instead of a stage."""
        if raw is None:
            return []
        if isinstance(raw, list):
            raw = "\n".join(str(item) for item in raw)
        values = []
        for line in raw.strip().splitlines():
            parsed = self._parse_sample_line(line)
            if parsed is None:
                log.warning(f"  iphone_export: bad quantity line: {line!r}")
                continue
            start, end, value_s = parsed
            try:
                values.append((start, end, float(value_s)))
            except ValueError:
                log.warning(f"  iphone_export: non-numeric quantity: {value_s!r}")
        return values

    def _activity_from_lines(self, raw) -> list[Chunk]:
        """Sum hourly-grouped step lines into the contract's activity entries."""
        hourly: dict[datetime, float] = {}
        for start, _end, value in self._quantity_lines(raw):
            hour = start.astimezone(self._local_tz).replace(minute=0, second=0, microsecond=0)
            hourly[hour] = hourly.get(hour, 0) + value
        entries = [
            {"hour": hour.isoformat(), "steps": int(total)}
            for hour, total in sorted(hourly.items()) if total
        ]
        return self._activity_chunks(entries)

    def _vitals_from_lines(self, resting_raw, hrv_raw, window_start: datetime) -> list[Chunk]:
        """Build the contract's vitals section from resting HR and HRV sample lines."""
        vitals = {}
        resting = self._quantity_lines(resting_raw)
        if resting:
            vitals["resting_hr"] = resting[-1][2]
            vitals["time"] = resting[-1][0].astimezone(self._local_tz).isoformat()
        hrv = [v for _s, _e, v in self._quantity_lines(hrv_raw)]
        if hrv:
            vitals["hrv_ms"] = sum(hrv) / len(hrv)
        return self._vitals_chunks(vitals, window_start)

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

