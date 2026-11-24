"""Tests for scrape.py, run from the repository root:

    python3 -m unittest discover -s .github/scripts -v

Every test works in a throwaway directory, so the real data/ is never touched.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest import mock
from xml.sax.saxutils import escape

import scrape


def feed_xml(feed, rows, prepared="2026-11-24T11:00:00+01:00") -> bytes:
    """Build a payload: one <postaja> per (sifra, period, name, values)."""
    stations = "".join(
        f"<postaja sifra='{sifra}'>"
        f"<merilno_mesto>{name}</merilno_mesto>"
        "<d96_e>1</d96_e><d96_n>2</d96_n>"
        "<wgs84_sirina>46</wgs84_sirina><wgs84_dolzina>14</wgs84_dolzina>"
        "<nadm_visina>300</nadm_visina>"
        f"<{feed.date_tag}>{period}</{feed.date_tag}>"
        + "".join(f"<{c}>{escape(values.get(c, ''))}</{c}>" for c in feed.column_names)
        + "</postaja>"
        for sifra, period, name, values in rows
    )
    header = f"<arsopodatki><datum_priprave>{prepared}</datum_priprave>"
    return f"{header}{stations}</arsopodatki>".encode()


def hour_row(period: str, sifra: str) -> tuple[str, str, dict[str, str]]:
    values = dict.fromkeys(scrape.URNI.column_names, "")
    values["merilno_mesto"] = "Test"
    values["pm10"] = "1"
    return period, sifra, values


def at(hour: int, minute: int = 0, day: int = 24) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=scrape.TZ)


class ScrapeTest(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        (self.root / "data").mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        for patch in (
            mock.patch.object(scrape, "find_repo_root", return_value=self.root),
            mock.patch.object(scrape, "log"),
            mock.patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def merge(self, rows):
        """Merge the way run_feed does: merge, then write what changed."""
        snapshot = scrape.FeedSnapshot(feed=scrape.URNI, prepared_on_date="p", rows=rows)
        merged, added, new_periods, written_periods = scrape.merge_snapshot_rows(
            scrape.URNI, snapshot, self.root
        )
        for path, merged_rows in merged.items():
            scrape.write_csv(path, scrape.URNI.header, merged_rows)
        return added, new_periods, written_periods

    def test_target_period_waits_for_the_delay(self):
        self.assertEqual(
            scrape.get_target_period_iso(scrape.URNI, at(10, 30)), "2026-11-24T09:00:00+01:00"
        )
        self.assertEqual(
            scrape.get_target_period_iso(scrape.URNI, at(10, 50)), "2026-11-24T10:00:00+01:00"
        )

    def test_daily_target_waits_for_the_feed_until_0250(self):
        self.assertEqual(scrape.get_target_period_iso(scrape.DNEVNI, at(0, 30)), "2026-11-22")
        self.assertEqual(scrape.get_target_period_iso(scrape.DNEVNI, at(1, 0)), "2026-11-22")
        self.assertEqual(scrape.get_target_period_iso(scrape.DNEVNI, at(2, 45)), "2026-11-22")
        self.assertEqual(scrape.get_target_period_iso(scrape.DNEVNI, at(2, 50)), "2026-11-23")
        self.assertEqual(scrape.get_target_period_iso(scrape.DNEVNI, at(3, 0)), "2026-11-23")

    def test_parse_keeps_markers_and_empty_cells(self):
        payload = feed_xml(
            scrape.URNI,
            [
                ("0001", "2026-11-23 10:00", "Test", {"pm10": "1", "pm2.5": "<1"}),
                ("0002", "2026-11-23 11:00", "Test", {"pm10": ""}),
            ],
        )

        snapshot = scrape.parse_xml_feed(scrape.URNI, payload)

        self.assertEqual(len(snapshot.rows), 2)
        self.assertEqual(snapshot.rows[0][2]["pm2.5"], "<1")
        self.assertEqual(snapshot.rows[1][2]["pm10"], "")
        self.assertEqual(set(snapshot.merilna_mesta), {"0001", "0002"})
        self.assertEqual(snapshot.latest_period_iso, "2026-11-23T11:00:00+01:00")

    def test_parse_rejects_a_feed_without_rows(self):
        with self.assertRaises(scrape.InvalidFeed) as caught:
            scrape.parse_xml_feed(
                scrape.URNI, b"<arsopodatki><datum_priprave>x</datum_priprave></arsopodatki>"
            )
        self.assertEqual(str(caught.exception), "no records in feed")

    def test_write_csv_only_when_content_changes(self):
        path = self.root / "data" / "probe.csv"

        self.assertTrue(scrape.write_csv(path, ["a"], [{"a": "1"}]))
        self.assertFalse(scrape.write_csv(path, ["a"], [{"a": "1"}]))
        self.assertTrue(scrape.write_csv(path, ["a"], [{"a": "2"}]))

    def test_merge_skips_known_rows_and_counts_topups_as_written(self):
        first = "2026-11-23T09:00:00+01:00"
        second = "2026-11-23T10:00:00+01:00"

        added, new, written = self.merge([hour_row(first, "1"), hour_row(second, "1")])
        self.assertEqual((added, len(new), len(written)), (2, 2, 2))

        added, new, written = self.merge([hour_row(first, "1"), hour_row(second, "1")])
        self.assertEqual((added, len(new), len(written)), (0, 0, 0))

        added, new, written = self.merge([hour_row(first, "9")])
        self.assertEqual((added, len(new), len(written)), (1, 0, 1))

    def test_registry_counts_added_changed_removed(self):
        def station(name="Ime"):
            return {**dict.fromkeys(scrape.ATTRS, "x"), "merilno_mesto": name}

        scrape.write_csv(
            self.root / "data" / "merilna-mesta.csv",
            ["sifra"] + list(scrape.ATTRS),
            [{"sifra": sifra, **station()} for sifra in ("0001", "0002", "0003")],
        )
        snapshot = scrape.FeedSnapshot(
            feed=scrape.URNI,
            prepared_on_date="p",
            merilna_mesta={"0001": station(), "0002": station("Novo"), "0004": station()},
        )

        self.assertEqual(scrape.update_merilna_mesta([snapshot]), (1, 1, 1))
        self.assertEqual(scrape.update_merilna_mesta([snapshot]), (0, 0, 0))

    def test_commit_message_lists_periods_rows_and_station_counts(self):
        hourly = scrape.FeedSnapshot(
            feed=scrape.URNI,
            prepared_on_date="p",
            changed=["data/urni/2026-11.csv"],
            written_periods={"2026-11-23T11:00:00+01:00", "2026-11-24T10:00:00+01:00"},
        )
        daily = scrape.FeedSnapshot(
            feed=scrape.DNEVNI,
            prepared_on_date="p",
            changed=["data/dnevni/2026-11.csv"],
            written_periods={"2026-11-23"},
        )

        def fake_run(feed, started_at):
            return hourly if feed is scrape.URNI else daily

        with (
            mock.patch.object(scrape, "run_feed", side_effect=fake_run),
            mock.patch.object(scrape, "update_merilna_mesta", return_value=(2, 1, 1)),
        ):
            self.assertEqual(scrape.main(), 0)

        self.assertEqual(
            (self.root / "commit-msg.txt").read_text(),
            "Data updated\n\n"
            "urni:\n"
            "2026-11-23 11:00 ... 2026-11-24 10:00 (2 periods)\n\n"
            "dnevni:\n"
            "2026-11-23\n\n"
            "merilna-mesta:\n"
            "2 added\n"
            "1 changed\n"
            "1 removed\n",
        )

    def test_idle_run_leaves_no_commit_message(self):
        message = self.root / "commit-msg.txt"
        message.write_text("Update data\n\nstale\n")
        idle = scrape.FeedSnapshot(feed=scrape.URNI, prepared_on_date="p")

        with (
            mock.patch.object(scrape, "run_feed", return_value=idle),
            mock.patch.object(scrape, "update_merilna_mesta", return_value=(0, 0, 0)),
        ):
            self.assertEqual(scrape.main(), 0)

        self.assertFalse(message.exists())


if __name__ == "__main__":
    unittest.main()
