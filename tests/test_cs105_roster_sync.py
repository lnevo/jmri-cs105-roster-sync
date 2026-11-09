"""Tests for the parts of cs105_roster_sync.py that run without JMRI."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "cs105_roster_sync.py"
SAMPLE = ROOT / "tests" / "sample_roster"
spec = importlib.util.spec_from_file_location("cs105_roster_sync", SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)

LOCO = """<?xml version="1.0" encoding="UTF-8"?>
<locomotive-config>
  <locomotive id="{id}" fileName="{file}" dccAddress="{addr}" comment="{comment}" roadName="{road}" roadNumber="{number}">
    <locoaddress><dcclocoaddress number="{addr}" longaddress="{long}" /></locoaddress>
    <functionlabels>{labels}</functionlabels>
  </locomotive>
</locomotive-config>
"""


def label(num, text, lockable="true"):
    return '<functionlabel num="%d" lockable="%s" visible="true">%s</functionlabel>' % (num, lockable, text)


class Encoding(unittest.TestCase):
    def test_node_ids_match_a_live_cs105(self):
        # Node IDs seen on a real CS-105 through JMRI.
        self.assertEqual(sync.train_node_id(1827, True), "06.01.00.00.C7.23")
        self.assertEqual(sync.train_node_id(2121, True), "06.01.00.00.C8.49")
        self.assertEqual(sync.train_node_id(9602, True), "06.01.00.00.E5.82")
        self.assertEqual(sync.train_node_id(6, False), "06.01.00.00.00.06")
        self.assertEqual(sync.train_node_id(6, True), "06.01.00.00.C0.06")

    def test_search_event_layout(self):
        # 09.00.99.FF + 6 BCD nibbles padded with F + flags
        # flags: allocate 80 | exact 40 | address-only 20 | DCC 08 | long 04
        self.assertEqual(sync.search_event(1827, True), "09.00.99.FF.18.27.FF.EC")
        self.assertEqual(sync.search_event(6, False), "09.00.99.FF.6F.FF.FF.E8")
        self.assertEqual(sync.search_event(572, True, allocate=False), "09.00.99.FF.57.2F.FF.6C")
        with self.assertRaises(ValueError):
            sync.search_event(1234567, True)

    def test_display_codes_follow_jmri(self):
        self.assertEqual(sync.display_code("Bell"), 13)
        self.assertEqual(sync.display_code(" horn "), 14)
        self.assertEqual(sync.display_code("Dynamic Brake"), 41)
        self.assertEqual(sync.display_code("Class Lights"), 0)
        self.assertEqual(sync.display_code(None), 0)

    def test_node_kinds(self):
        self.assertTrue(sync.is_train_node("06.01.00.00.C7.23"))
        self.assertFalse(sync.is_train_node("09.00.99.03.01.68"))
        self.assertTrue(sync.is_cs105_model("CS-105"))
        self.assertTrue(sync.is_cs105_model("TCS CS-105 Command Station"))
        self.assertFalse(sync.is_cs105_model("UWT-100"))
        self.assertFalse(sync.is_cs105_model(None))

    def test_firmware_spellings_get_the_same_icons(self):
        self.assertEqual(sync.display_code("Shunt"), sync.display_code("Shunting Mode"))
        self.assertEqual(sync.display_code("Blowdown"), sync.display_code("Blow Down"))
        # codes JMRI's export does not use stay plain text until tested on a CS-105
        self.assertEqual(sync.display_code("Mars Light"), 0)


class CommandStation(unittest.TestCase):
    CS = "02.01.57.10.00.01"
    OTHER = "05.01.01.01.22.00"

    def test_event_is_the_tcs_command_station_event(self):
        self.assertEqual(sync.COMMAND_STATION_EVENT, "09.00.99.FE.FF.FF.00.01")

    def test_found_by_event_whatever_the_model(self):
        s = sync.choose_command_station([self.OTHER.lower()], {self.CS: "CS-105"})
        self.assertEqual(s, {"node": self.OTHER, "by": "event", "all": [self.OTHER]})
        self.assertIn("produces the command-station event", sync.station_status(s))

    def test_falls_back_to_cs105_model_name(self):
        s = sync.choose_command_station([], {self.CS: "TCS CS-105", self.OTHER: "UWT-100"})
        self.assertEqual((s["node"], s["by"]), (self.CS, "model"))
        self.assertIn("did not answer", sync.station_status(s))

    def test_none_found(self):
        s = sync.choose_command_station([], {self.OTHER: "UWT-100"})
        self.assertEqual(s, {"node": None, "by": None, "all": []})
        self.assertEqual(sync.station_status(s), "command station NOT FOUND")

    def test_two_answers_refuse_writing(self):
        s = sync.choose_command_station([self.CS, self.OTHER], {})
        self.assertIsNone(s["node"])
        self.assertEqual(s["all"], [self.CS, self.OTHER])
        self.assertIn("2 command stations", sync.station_status(s))

    def test_train_nodes_are_never_the_command_station(self):
        s = sync.choose_command_station(["06.01.00.00.C7.23"], {"06.01.00.00.C8.49": "CS-105"})
        self.assertIsNone(s["node"])


class RosterFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "roster").mkdir()
        labels = label(0, "Light") + label(1, "Bell") + label(2, "Horn", "false") + label(3, "Coupler")
        (root / "roster" / "18270.xml").write_text(LOCO.format(
            id="1827", file="18270.xml", addr=1827, comment="ES44AH", long="yes", labels=labels,
            road="CSX", number="1827"))
        (root / "roster" / "060.xml").write_text(LOCO.format(
            id="06L", file="060.xml", addr=6, comment="", long="yes", labels="", road="", number=""))
        (root / "roster" / "stale.xml").write_text(LOCO.format(
            id="stale", file="stale.xml", addr=99, comment="", long="no", labels="", road="X", number="9"))
        (root / "roster.xml").write_text(
            '<roster-config><roster>'
            '<locomotive id="1827" fileName="18270.xml" dccAddress="1827"/>'
            '<locomotive id="06L" fileName="060.xml" dccAddress="6"/>'
            '</roster></roster-config>')
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def entries(self):
        return sync.load_roster(self.root / "roster.xml", self.root / "roster")

    def test_only_indexed_files_are_loaded(self):
        self.assertEqual([e["id"] for e in self.entries()], ["1827", "06L"])

    def test_entry_fields(self):
        e = self.entries()[0]
        self.assertEqual((e["address"], e["long"], e["comment"]), (1827, True, "ES44AH"))
        self.assertEqual(e["labels"][2], ("Horn", False))
        self.assertTrue(self.entries()[1]["long"])

    def test_desired_values_shift_f1_to_functions0(self):
        v = sync.desired_values(self.entries()[0], include_name=True)
        self.assertEqual(v["Train.Name"], "1827")
        self.assertEqual(v["Train.User Description"], "ES44AH")
        # F1 Bell -> Functions(0) icon 13, no Description written
        self.assertEqual(v["Train.Functions(0).Display"], 13)
        self.assertNotIn("Train.Functions(0).Description", v)
        # F2 Horn, not lockable -> Momentary 1
        self.assertEqual(v["Train.Functions(1).Momentary"], 1)
        # F3 Coupler has no icon code -> plain text
        self.assertEqual(v["Train.Functions(2).Display"], 0)
        self.assertEqual(v["Train.Functions(2).Description"], "Coupler")
        # unlabelled slots are cleared, not left out
        self.assertEqual(v["Train.Functions(27).Description"], "")
        self.assertEqual(v["Train.Functions(27).Momentary"], 0)
        self.assertNotIn("Train.Name", sync.desired_values(self.entries()[0], include_name=False))

    def test_empty_comment_leaves_description_alone(self):
        v = sync.desired_values(self.entries()[1], include_name=False)
        self.assertNotIn("Train.User Description", v)

    def test_plan_create_update_only_and_rename(self):
        existing = ["06.01.00.00.C0.06", "06.01.00.00.00.03", "09.00.99.03.01.68"]
        actions = sync.plan(self.entries(), existing)
        self.assertEqual([(a["entry"]["id"], a["action"]) for a in actions],
                         [("1827", "create"), ("06L", "update")])
        self.assertEqual(actions[0]["event"], "09.00.99.FF.18.27.FF.EC")
        self.assertIn("Train.Name", actions[0]["values"])
        self.assertNotIn("Train.Name", actions[1]["values"])
        self.assertEqual(sync.summary(actions), {"create": 1, "update": 1, "skip": 0})
        renamed = sync.plan(self.entries(), existing, rename=True)
        self.assertIn("Train.Name", renamed[1]["values"])
        only = sync.plan(self.entries(), existing, only={"06L"})
        self.assertEqual([a["entry"]["id"] for a in only], ["06L"])
        self.assertEqual(sync.unmanaged_train_nodes(existing, self.entries()), ["06.01.00.00.00.03"])
        # a run limited with Only must not report the other roster entries as unmanaged
        self.assertEqual(sync.unmanaged_train_nodes(existing + ["06.01.00.00.C7.23"], self.entries()),
                         ["06.01.00.00.00.03"])

    def test_unknown_only_ids_are_reported(self):
        self.assertEqual(sync.unknown_ids(self.entries(), {"1827", "2121"}), ["2121"])
        self.assertEqual(sync.unknown_ids(self.entries(), None), [])

    def test_name_schemes(self):
        e1827, e06l = self.entries()
        self.assertEqual(sync.train_name(e1827), "1827")
        self.assertEqual(sync.train_name(e1827, "road"), "CSX 1827")
        # no road data -> fall back to the roster ID
        self.assertEqual(sync.train_name(e06l, "road"), "06L")
        with self.assertRaises(ValueError):
            sync.train_name(e1827, "bogus")
        actions = sync.plan(self.entries(), ["06.01.00.00.C7.23"], rename=True, only={"1827"}, names="road")
        self.assertEqual(actions[0]["values"]["Train.Name"], "CSX 1827")
        self.assertIn("name -> 'CSX 1827'", sync.describe(actions[0]))

    def test_describe_and_parse_only(self):
        a = sync.plan(self.entries(), [])[0]
        self.assertIn("CREATE 1827", sync.describe(a))
        self.assertEqual(sync.parse_only("1827, 572"), {"1827", "572"})
        self.assertIsNone(sync.parse_only(""))
        self.assertIsNone(sync.parse_only(" , "))


class Options(unittest.TestCase):
    def test_no_options_means_interactive(self):
        self.assertIsNone(sync.options_from({"other": 1}))

    def test_options_are_read_and_removed(self):
        g = {"CS105_APPLY": True, "CS105_ONLY": "572", "CS105_NAMES": "road", "keep": 1}
        opts = sync.options_from(g)
        self.assertEqual(opts, {"apply": True, "only": "572", "rename": False,
                                "names": "road", "connection": None})
        # removed, so a later menu run does not inherit APPLY
        self.assertEqual(g, {"keep": 1})
        self.assertIsNone(sync.options_from(g))

    def test_bad_name_scheme_is_refused(self):
        with self.assertRaises(ValueError):
            sync.options_from({"CS105_NAMES": "number"})

    def test_pick_connection(self):
        one = [("L", "LCC")]
        two = [("L", "LCC"), ("M", "LCC test")]
        self.assertEqual(sync.pick_connection(one), 0)
        self.assertEqual(sync.pick_connection(two, "M"), 1)
        self.assertEqual(sync.pick_connection(two, "LCC test"), 1)
        with self.assertRaises(ValueError):
            sync.pick_connection(two)
        with self.assertRaises(ValueError):
            sync.pick_connection(two, "X")
        with self.assertRaises(ValueError):
            sync.pick_connection([])


class SampleRoster(unittest.TestCase):
    """The roster in tests/sample_roster, as JMRI 5 writes it."""

    def test_sample_plan(self):
        entries = sync.load_roster(SAMPLE / "roster.xml", SAMPLE / "roster")
        self.assertEqual([e["id"] for e in entries], ["Demo 101", "Demo 7", "No Address"])
        actions = sync.plan(entries, ["06.01.00.00.C0.65"], names="road")
        self.assertEqual([a["action"] for a in actions], ["update", "create", "skip"])
        self.assertEqual(actions[1]["values"]["Train.Name"], "JF&N 7")
        self.assertEqual(actions[1]["node"], "06.01.00.00.00.07")

    def test_cli_runs(self):
        out = subprocess.run([sys.executable, str(SCRIPT), str(SAMPLE), "--only", "Demo 7,Typo"],
                             capture_output=True, text=True, check=True).stdout
        self.assertIn("CREATE Demo 7", out)
        self.assertIn("'Typo' is not a roster ID", out)


if __name__ == "__main__":
    unittest.main()
