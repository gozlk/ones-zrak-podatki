#!/usr/bin/env python3
"""ARSO air quality scraper — hourly and daily feeds into monthly CSVs.

Sources
    https://www.arso.gov.si/xml/zrak/ones_zrak_urni_podatki_7dni.xml
    https://www.arso.gov.si/xml/zrak/ones_zrak_dnevni_podatki_zadnji.xml

Layout
    data/urni/YYYY-MM.csv         hourly, one row per station and hour
    data/dnevni/YYYY-MM.csv       daily aggregates
    data/merilna-mesta.csv        stations, built from the feeds

Design
  1. Rows are keyed by the feed's own period, never by wall clock.
  2. A period is read at the time the feed declares in <predlagan_zajem>:
     45 minutes past the hour for the hourly feed, 02:45 for the daily
     aggregates. The pattern only rounds that up to the next scheduled run,
     so each period is written once.
  3. A written row is never modified: an existing (period, sifra) key is
     skipped, so a station that appears late is appended but its earlier values
     stay as they were.
  4. A file is only rewritten when its content actually changes, and it is
     replaced atomically, so a killed run never leaves a truncated CSV.

The 7-day feed keeps every hour for a week, so a missed run costs nothing: the
next run re-reads the last 24 hours and picks up whatever was skipped.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Ljubljana")
UTC = ZoneInfo("UTC")
ATTRS = (
    "merilno_mesto",
    "d96_e",
    "d96_n",
    "wgs84_sirina",
    "wgs84_dolzina",
    "nadm_visina",
)
MERILNA_MESTA_FILE = "merilna-mesta.csv"

# A plain number, or a below-detection-limit marker such as "<1".
VALUE_RE = re.compile(r"^<?\d+(\.\d+)?$")


class InvalidFeed(Exception):
    pass


@dataclass(frozen=True)
class Feed:
    # Display label for the commit message, e.g. "Urni podatki".
    name: str
    # Folder under data/ and the log label.
    path_name: str
    url: str
    key_column: str
    date_tag: str
    # True if feed has one row per hour.
    is_hourly: bool
    # How much history to read, in days of periods ending at the target: 24
    # hourly rows or one daily row per day. None means only the latest period
    # is readable, so there is nothing to backfill.
    history_days: int | None
    # When a period may be read, relative to its own boundary: "H+49" — an hour,
    # 49 minutes after it closes; "00:00+170" — a day, 170 minutes after the
    # midnight that follows it, which is 02:50. Each delay is the feed's own
    # <predlagan_zajem> rounded up to the next :05/:20/:35/:50 run: the hourly
    # feed asks for 45 minutes past the hour, the daily one for 02:45.
    scrape_delay_pattern: str
    column_names: tuple[str, ...]

    @property
    def header(self) -> list[str]:
        return [self.key_column, "sifra", "merilno_mesto"] + list(self.column_names)


@dataclass
class FeedSnapshot:
    feed: Feed
    prepared_on_date: str
    rows: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    # Keyed by sifra: the feed repeats a postaja once per row, so a list would
    # hold the same station once per hour instead of once per station.
    merilna_mesta: dict[str, dict[str, str]] = field(default_factory=dict)
    latest_period_iso: str | None = None
    # Periods this run appended, for the commit message; the feed's own latest
    # period is not necessarily what was written.
    written_periods: set[str] = field(default_factory=set)
    changed: list[str] = field(default_factory=list)


URNI = Feed(
    name="Urni podatki",
    path_name="urni",
    url="https://www.arso.gov.si/xml/zrak/ones_zrak_urni_podatki_7dni.xml",
    key_column="datum_cas",
    date_tag="datum_do",
    is_hourly=True,
    history_days=1,
    scrape_delay_pattern="H+49",
    column_names=("pm10", "pm2.5", "o3", "no2", "nox", "so2", "co", "benzen"),
)

DNEVNI = Feed(
    name="Dnevni podatki",
    path_name="dnevni",
    url="https://www.arso.gov.si/xml/zrak/ones_zrak_dnevni_podatki_zadnji.xml",
    key_column="datum",
    date_tag="datum",
    is_hourly=False,
    history_days=1,
    scrape_delay_pattern="00:00+170",
    column_names=(
        "pm10_dnevna",
        "pm2.5_dnevna",
        "co_max_8urna",
        "o3_max_urna",
        "o3_max_8urna",
        "no2_max_urna",
        "so2_dnevna",
        "so2_max_urna",
    ),
)

# Feeds and order of processing.
FEEDS = (URNI, DNEVNI)


def log(label: str, msg: str) -> None:
    """Print one line as [timestamp] [label] message."""
    now = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{now} [{label}] {msg}", flush=True)


def find_repo_root(start: str | os.PathLike[str] | None = None) -> Path:
    """Walk up from `start`, by default this file's folder, until .git is found."""
    here = Path(__file__).parent if start is None else Path(start)
    here = here.resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return here


def parse_csv_file(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read a CSV file and return its header and rows as a list of dicts."""
    if not path.exists():
        return [], []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, header: list[str], rows: list[dict[str, str]]) -> bool:
    """Write CSV only when content changed, atomically. Returns True on change."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows([row.get(col, "") for col in header] for row in rows)
    body = buf.getvalue()

    if path.exists() and path.read_text(encoding="utf-8") == body:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        mode="w",
        encoding="utf-8",
        newline="",
        delete=False,
    ) as fh:
        fh.write(body)
        tmp = fh.name
    try:
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def fetch(url: str) -> bytes:
    """Download a URL with custom User-Agent and 2-minute timeout, returning the raw bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": "ones-zrak-podatki scraper"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def feed_monthly_csv_path(feed: Feed, date: str, out_root: Path) -> Path:
    """Return the path to the CSV file for this feed in format YYYY-MM."""
    return out_root / "data" / feed.path_name / f"{date[:7]}.csv"


def parse_feed_period_iso(feed: Feed, raw_date: str, sifra: str) -> str:
    """Parse one row's date in the feed's own format into ISO 8601."""
    raw_date = raw_date.strip()
    fmt = "%Y-%m-%d %H:%M" if feed.is_hourly else "%Y-%m-%d"
    try:
        parsed = datetime.strptime(raw_date, fmt)
    except ValueError as e:
        raise InvalidFeed(f"{sifra} {feed.date_tag}={raw_date!r}: {e}") from e
    if feed.is_hourly:
        return parsed.replace(tzinfo=TZ).isoformat()
    else:
        return parsed.date().isoformat()


def parse_xml_feed(feed: Feed, payload: bytes) -> FeedSnapshot:
    """Parse the XML feed into a FeedSnapshot, validating its structure and values."""
    root = ET.fromstring(payload)
    if root.tag != "arsopodatki":
        raise InvalidFeed(f"unexpected root <{root.tag}>")

    prepared_on_date = (root.findtext("datum_priprave") or "").strip()
    if not prepared_on_date:
        raise InvalidFeed("missing <datum_priprave>")

    snapshot = FeedSnapshot(feed=feed, prepared_on_date=prepared_on_date)

    for row in root.findall("postaja"):
        sifra = (row.get("sifra") or "").strip()
        if not sifra:
            continue
        row_raw_date = (row.findtext(feed.date_tag) or "").strip()
        if not row_raw_date:
            continue

        item_period_iso = parse_feed_period_iso(feed, row_raw_date, sifra)

        if snapshot.latest_period_iso is None or item_period_iso > snapshot.latest_period_iso:
            snapshot.latest_period_iso = item_period_iso

        attrs = {
            a: ((row.findtext(a) if a == "merilno_mesto" else row.get(a)) or "").strip()
            for a in ATTRS
        }
        snapshot.merilna_mesta[sifra] = attrs

        values = {"merilno_mesto": attrs["merilno_mesto"]}
        for col in feed.column_names:
            raw = (row.findtext(col) or "").strip()
            if raw == "":
                values[col] = ""
                continue
            if not VALUE_RE.match(raw):
                raise InvalidFeed(f"{sifra} {col}={raw!r} not a value")
            if not raw.startswith("<") and float(raw) < 0:
                raise InvalidFeed(f"{sifra} {col}={raw} negative")
            values[col] = raw
        snapshot.rows.append((item_period_iso, sifra, values))

    if not snapshot.rows:
        raise InvalidFeed("no records in feed")

    seen: dict[str, set[str]] = {}
    for item_period_iso, sifra, _rec in snapshot.rows:
        bucket = seen.setdefault(item_period_iso, set())
        if sifra in bucket:
            raise InvalidFeed(f"duplicate 'sifra' {sifra} in window {item_period_iso}")
        bucket.add(sifra)

    return snapshot


def merge_snapshot_rows(
    feed: Feed, snapshot: FeedSnapshot, out_root: Path
) -> tuple[dict[Path, list[dict[str, str]]], int, set[str], set[str]]:
    """Append rows to the monthly CSVs, skipping duplicates and validating headers.

    Returns the merged rows per file, the row count added, the set of periods
    that did not exist in the file before — a period that only gains a missing
    station is topped up but not counted as new — and the set of periods this
    run appended, new or topped up.
    """
    rows_by_path: dict[Path, list[dict[str, str]]] = {}
    known_keys: dict[Path, set[tuple[str, str]]] = {}
    known_periods: dict[Path, set[str]] = {}
    added_rows = 0
    new_periods: set[str] = set()
    written_periods: set[str] = set()

    for period, sifra, values in snapshot.rows:
        path = feed_monthly_csv_path(feed, period, out_root)
        if path not in rows_by_path:
            header, existing_rows = parse_csv_file(path)
            if header and header != feed.header:
                raise InvalidFeed(
                    f"{path}: header mismatch, refusing to merge\n"
                    f"    have: {header}\n    want: {feed.header}"
                )
            rows_by_path[path] = existing_rows
            known_keys[path] = {(row[feed.key_column], row["sifra"]) for row in existing_rows}
            known_periods[path] = {row[feed.key_column] for row in existing_rows}
        key = (period, sifra)

        # Skip existing rows.
        if key in known_keys[path]:
            continue

        row = {
            feed.key_column: period,
            "sifra": sifra,
            "merilno_mesto": values["merilno_mesto"],
        }
        row.update({c: values[c] for c in feed.column_names})
        rows_by_path[path].append(row)
        known_keys[path].add(key)
        if period not in known_periods[path]:
            new_periods.add(period)
            known_periods[path].add(period)
        written_periods.add(period)
        added_rows += 1

    for rows in rows_by_path.values():
        rows.sort(key=lambda row: (row[feed.key_column], row["sifra"]))
    return rows_by_path, added_rows, new_periods, written_periods


def get_target_period_iso(feed: Feed, now: datetime) -> str:
    """Return the newest closed period that is ready to be read from the feed, as ISO 8601,
    using the feed's scrape delay pattern to find the right target."""
    base, delay_text = feed.scrape_delay_pattern.split("+")
    delay = timedelta(minutes=int(delay_text))

    if base == "H":
        hour = now.replace(minute=0, second=0, microsecond=0)
        return (hour if now - hour >= delay else hour - timedelta(hours=1)).isoformat()

    publish_time = datetime.strptime(base, "%H:%M").time()
    date = now.date()
    while datetime.combine(date, publish_time, tzinfo=now.tzinfo) + timedelta(days=1) + delay > now:
        date -= timedelta(days=1)
    return date.isoformat()


def run_feed(feed: Feed, started_at: datetime) -> FeedSnapshot | None:
    """Fetch, parse, and merge one feed into the monthly CSVs and return a snapshot."""
    out_root = find_repo_root()
    log_label = f"run-feed:{feed.path_name}"

    try:
        payload = fetch(feed.url)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log(log_label, f"DOWNLOAD FAILED: {exc}")
        return None

    sha = hashlib.sha256(payload).hexdigest()[:16]
    try:
        snapshot = parse_xml_feed(feed, payload)
    except (InvalidFeed, ET.ParseError) as exc:
        log(log_label, f"INVALID DATA: {exc} | sha: {sha}")
        return None

    target_period_iso = get_target_period_iso(feed, started_at)
    if feed.history_days is None:
        oldest_period = target_period_iso
    elif feed.is_hourly:
        oldest_period = (
            datetime.fromisoformat(target_period_iso) - timedelta(hours=feed.history_days * 24 - 1)
        ).isoformat()
    else:
        oldest_period = (
            (datetime.fromisoformat(target_period_iso) - timedelta(days=feed.history_days - 1))
            .date()
            .isoformat()
        )

    # latest_fetched_period = max((row[0] for row in snapshot.rows), default="-")
    latest_fetched_period = snapshot.latest_period_iso or "-"

    target_rows = [row for row in snapshot.rows if oldest_period <= row[0] <= target_period_iso]
    snapshot.rows = target_rows

    if not target_rows:
        log(
            log_label,
            f"no data for target period {target_period_iso} yet, latest: {latest_fetched_period}, "
            f"feed_prepared_on: {snapshot.prepared_on_date} | sha: {sha}",
        )
        return snapshot

    try:
        rows_by_path, added_rows, new_periods, written_periods = merge_snapshot_rows(
            feed, snapshot, out_root
        )
    except InvalidFeed as exc:
        log(log_label, f"INVALID DATA: {exc}")
        return None

    changed = [
        os.path.relpath(path, out_root)
        for path, rows in rows_by_path.items()
        if write_csv(path, feed.header, rows)
    ]
    snapshot.changed = changed
    snapshot.written_periods = written_periods

    file_status = f"file written: {','.join(changed)}" if changed else "file unchanged"
    log(
        log_label,
        f"target_period: {target_period_iso}"
        f", feed_prepared_on: {snapshot.prepared_on_date}"
        f", new_rows_count: {added_rows}"
        f", new_periods_count: {len(new_periods)}"
        f", {file_status}",
    )
    return snapshot


def update_merilna_mesta(snapshots: list[FeedSnapshot]) -> tuple[int, int, int]:
    """Rewrite csv of merilna mesta and return the (added, changed, removed) counts.

    Unlike the monthly fact tables, this file is overwritten in place: it holds
    the current state. A site that is renamed or relocated keeps its `sifra` and
    its old values disappear, and a site the feed no longer reports is dropped.
    The history of those changes lives in git, not here.
    """
    path = find_repo_root() / "data" / MERILNA_MESTA_FILE
    header = ["sifra"] + list(ATTRS)
    _, rows = parse_csv_file(path)
    index = {row["sifra"]: row for row in rows}

    # Get the first snapshot that has merilna_mesta, if any.
    snapshot = next((item for item in snapshots if item.merilna_mesta), None)
    if snapshot is None:
        return 0, 0, 0

    notes: list[str] = []
    added_sites = 0
    changed_sites = 0
    for sifra, attrs in snapshot.merilna_mesta.items():
        existing = index.get(sifra)
        if existing:
            diff = [a for a in ATTRS if (existing.get(a) or "") != (attrs.get(a) or "")]
            if diff:
                # A rename or move is rare, so spell it out; an added or dropped
                # station is a whole CSV row, which `git show` already lists.
                detail = ", ".join(f"{a}: {existing.get(a)!r} -> {attrs[a]!r}" for a in diff)
                notes.append(f"station {sifra} changed: {detail}")
                existing.update(attrs)
                changed_sites += 1
        else:
            index[sifra] = {"sifra": sifra, **attrs}
            added_sites += 1

    removed_sites = 0
    for sifra in sorted(set(index) - set(snapshot.merilna_mesta)):
        del index[sifra]
        removed_sites += 1

    merged_rows = [index[sifra] for sifra in sorted(index)]
    changed = write_csv(path, header, merged_rows)
    detail = "".join(f"\n    {note}" for note in notes)
    log(
        "update:merilna-mesta",
        f"added: {added_sites}, changed: {changed_sites}, removed: {removed_sites}, "
        f"{'file written' if changed else 'file unchanged'}{detail}",
    )
    return added_sites, changed_sites, removed_sites


def main() -> int:
    """Run both feeds. No arguments: the repository is found from this file and
    the commit message goes to $RUNNER_TEMP (or the system temp dir)."""
    started_at = datetime.now(tz=TZ)
    # Run each feed and collect snapshots, skipping any that failed.
    snapshots = [snapshot for feed in FEEDS if (snapshot := run_feed(feed, started_at)) is not None]
    snapshots_changed = any(snapshot.changed for snapshot in snapshots)

    # Check if merilna-mesta CSV needs to be updated based on the snapshots.
    added_sites, changed_sites, removed_sites = update_merilna_mesta(snapshots)
    merilna_mesta_changed = bool(added_sites or changed_sites or removed_sites)

    # The workflow commits only when this file exists, so it must be removed
    # when nothing changed: a stale message from an earlier run cannot survive.
    message_file = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "commit-msg.txt"

    # Build a commit message if there were any changes: a plain subject, then the
    # periods each feed wrote and what moved in merilna mesta.
    if snapshots_changed or merilna_mesta_changed:
        blocks: list[str] = []
        for snapshot in snapshots:
            if not snapshot.changed or not snapshot.written_periods:
                continue
            first_period = min(snapshot.written_periods)
            last_period = max(snapshot.written_periods)
            span_start, span_end = (
                p[:16].replace("T", " ") if "T" in p else p for p in (first_period, last_period)
            )
            span = span_start if span_start == span_end else f"{span_start} ... {span_end}"

            period_count = len(snapshot.written_periods)
            count = f" ({period_count} periods)" if period_count > 1 else ""
            blocks.append(f"{snapshot.feed.path_name}:\n{span}{count}")

        if merilna_mesta_changed:
            counts = [
                f"{count} {label}"
                for count, label in (
                    (added_sites, "added"),
                    (changed_sites, "changed"),
                    (removed_sites, "removed"),
                )
                if count
            ]
            if counts:
                blocks.append("merilna-mesta:\n" + "\n".join(counts))

        body = "\n\n".join(blocks)
        commit_message = f"Data updated\n\n{body}\n" if body else "Data updated\n"
        message_file.write_text(commit_message, encoding="utf-8")
        log("commit-message", commit_message.strip())
    else:
        message_file.unlink(missing_ok=True)

    return 0 if len(snapshots) == len(FEEDS) else 1


if __name__ == "__main__":
    sys.exit(main())
