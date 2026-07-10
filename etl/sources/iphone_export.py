"""Health data from the nightly iPhone Shortcuts export.

Reads $IPHONE_EXPORT_PATH/YYYY-MM-DD.json — one file per logical day,
POSTed by a Shortcuts automation and stored by the receiver service —
and emits chunks in the text formats the diary and query pipeline expect.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta

from .base import Chunk, fmt_duration

log = logging.getLogger(__name__)

# Shortcuts' default date rendering, e.g. "Jul 9, 2026 at 2:41 AM"
_LOCALE_DT_RE = re.compile(r"[A-Z][a-z]{2} \d{1,2}, \d{4} at \d{1,2}:\d{2} [AP]M")
_LOCALE_DT_FMT = "%b %d, %Y at %I:%M %p"

ACTIVE_STEPS_PER_HOUR = 500
STEPS_DEVIATION = 0.30

BASELINE_DAYS = 14
BASELINE_MIN_DAYS = 7


class IPhoneExportSource:
    def __init__(self, export_dir: str, local_tz):
        self._export_dir = export_dir
        self._local_tz = local_tz

    def get_chunks(self, start: datetime, end: datetime) -> list[Chunk]:
        date_str = start.strftime("%Y-%m-%d")
        data = self._load_export(date_str)
        if data is None:
            log.info(f"  iphone_export: no usable file for {date_str}")
            return []

        if data.get("date") not in (None, date_str):
            log.warning(f"  iphone_export: {date_str}.json says date={data['date']}, trusting filename")

        chunks = []
        if "sleep" in data:
            chunks.extend(self._sleep_samples_chunks(data["sleep"]))
        vitals = self._extract_vitals(data)
        if vitals:
            chunks.extend(self._vitals_chunks(vitals, start))
        if "activity" in data:
            hourly = self._hourly_steps_from_entries(data["activity"])
        else:
            hourly = self._hourly_steps_from_lines(data.get("step", data.get("steps")))
        chunks.extend(self._steps_chunks(hourly, start, None))
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

    def _hourly_steps_from_lines(self, raw) -> dict[datetime, float]:
        hourly: dict[datetime, float] = {}
        for start, _end, value in self._quantity_lines(raw):
            hour = start.astimezone(self._local_tz).replace(minute=0, second=0, microsecond=0)
            hourly[hour] = hourly.get(hour, 0) + value
        return hourly

    def _hourly_steps_from_entries(self, entries: list) -> dict[datetime, float]:
        hourly: dict[datetime, float] = {}
        for entry in entries:
            steps = entry.get("steps")
            if steps:
                hour = self._ts(entry["hour"])
                hourly[hour] = hourly.get(hour, 0) + steps
        return hourly

    def _steps_chunks(self, hourly: dict[datetime, float], day_start: datetime, baseline_mean: float | None) -> list[Chunk]:
        total = int(sum(hourly.values()))
        if not total:
            return []

        chunks = []
        active = sorted(h for h, v in hourly.items() if v >= ACTIVE_STEPS_PER_HOUR)
        stretch: list[datetime] = []
        for hour in active + [None]:
            if stretch and (hour is None or (hour - stretch[-1]).total_seconds() > 3600):
                first, last = stretch[0], stretch[-1] + timedelta(hours=1)
                steps = sum(hourly[h] for h in stretch)
                span = f"{first.strftime('%Y-%m-%d %H:%M')}–{last.strftime('%H:%M')}"
                chunks.append(Chunk(
                    window_start=first.isoformat(),
                    text=f"[{span}] Sustained movement: ~{round(steps, -2):,.0f} steps.",
                    apps=[], total_secs=3600 * len(stretch), source="iphone_export",
                    metadata={"kind": "steps"},
                ))
                stretch = []
            if hour is not None:
                stretch.append(hour)

        comparison = ""
        if baseline_mean and abs(total - baseline_mean) / baseline_mean > STEPS_DEVIATION:
            direction = "above" if total > baseline_mean else "below"
            comparison = f" — well {direction} recent average ({baseline_mean:,.0f}/day)"
        chunks.append(Chunk(
            window_start=day_start.isoformat(),
            text=f"[{day_start.strftime('%Y-%m-%d')}] Steps: {total:,} total{comparison}.",
            apps=[], total_secs=0, source="iphone_export",
            metadata={"kind": "steps"},
        ))
        return chunks

    def _load_export(self, date_str: str) -> dict | None:
        path = os.path.join(self._export_dir, f"{date_str}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"  iphone_export: unreadable {path}: {e}")
            return None

    def _extract_vitals(self, data: dict) -> dict:
        """Vitals values from either payload shape (contract dict or sample lines)."""
        if "vitals" in data:
            return data["vitals"]
        vitals = {}
        resting = self._quantity_lines(data.get("resting_hr"))
        if resting:
            vitals["resting_hr"] = resting[-1][2]
            vitals["time"] = resting[-1][0].astimezone(self._local_tz).isoformat()
        hrv = [v for _s, _e, v in self._quantity_lines(data.get("hrv"))]
        if hrv:
            vitals["hrv_ms"] = sum(hrv) / len(hrv)
        return vitals

    def _trailing_metrics(self, start: datetime) -> list[dict]:
        """Per-day steps/vitals values for the BASELINE_DAYS days before `start`."""
        metrics = []
        for offset in range(1, BASELINE_DAYS + 1):
            data = self._load_export((start - timedelta(days=offset)).strftime("%Y-%m-%d"))
            if data is None:
                continue
            if "activity" in data:
                hourly = self._hourly_steps_from_entries(data["activity"])
            else:
                hourly = self._hourly_steps_from_lines(data.get("step", data.get("steps")))
            vitals = self._extract_vitals(data)
            metrics.append({
                "steps": sum(hourly.values()) or None,
                "resting_hr": vitals.get("resting_hr"),
                "hrv_ms": vitals.get("hrv_ms"),
            })
        return metrics

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


