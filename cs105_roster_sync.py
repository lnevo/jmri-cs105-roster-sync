# cs105_roster_sync.py - copy the JMRI roster into a TCS CS-105 command station.
#
# https://github.com/lnevo/jmri-cs105-roster-sync   (GPL-2.0, like JMRI)
#
# Run it from JMRI: Scripting > Run Script..., pick this file. A window opens
# that shows the plan first and writes nothing until you press Apply.
#
# What it does, per roster entry (DCC address, long or short):
#   1. Works out the CS-105 train node for the address:
#      06.01.00.00.00.nn (short) or 06.01.00.00.(C0|hi).lo (long), the same
#      mapping JMRI's own LCC throttle uses.
#   2. If the CS-105 has no train for that address, asks it to create one with
#      an OpenLCB Train Search "Identify Producer" carrying the Allocate flag.
#   3. Writes the train's settings the way DecoderPro's
#      "File > Export > Roster entry to TCS CS-105" does: Train.Name (new trains
#      only, unless Rename is ticked), Train.User Description (the roster
#      comment, when there is one) and, for F1 to F28, the function label,
#      icon and momentary flag.
#
# Train nodes on the CS-105 with no roster entry are listed as KEEP and never
# changed. Consist Behavior is left alone. Nothing here moves a train, sets a
# turnout or touches track power.
#
# Scripted use (no window): set any of these names in the script's globals
# before it runs, for example by prepending lines to a copy of this file.
#   CS105_APPLY=True          write to the CS-105 (default False = plan only)
#   CS105_ONLY="1827,572"     only these roster IDs
#   CS105_RENAME=True         also set Train.Name on trains that already exist
#   CS105_NAMES="road"        name trains "<road name> <road number>" (default "id")
#   CS105_CONNECTION="L"      LCC connection to use, by system prefix or name,
#                             when JMRI has more than one
# The result is in RESULT. A refused run raises RuntimeError.
#
# Under plain Python 3 the file plans from roster files only (no device):
#   python3 cs105_roster_sync.py <folder with roster.xml and roster/> --values

import os
import sys
import time
import xml.etree.ElementTree as ET

VERSION = "1.0.0"

SEARCH_PREFIX = (0x09, 0x00, 0x99, 0xFF)
FLAG_ALLOCATE = 0x80
FLAG_EXACT = 0x40
FLAG_ADDRESS_ONLY = 0x20
PROTO_DCC = 0x08
DCC_LONG = 0x04
TRAIN_NODE_PREFIX = "06.01.00.00"
N_FUNCTIONS = 28  # Train.Functions(0..27) = F1..F28, as in the CS-105 CDI
CS105_MODEL = "CS-105"
LOCK_PROPERTY = "cs105.roster.sync.running"
OPTION_NAMES = ("CS105_APPLY", "CS105_ONLY", "CS105_RENAME", "CS105_NAMES", "CS105_CONNECTION")

# Label text -> CS-105 "Display" icon code. Copied from JMRI 5.16
# jmri.jmrit.symbolicprog.TcsExportAction.intFromFunctionString so both tools
# agree. Unknown text -> 0 and the text is sent as the Description instead.
DISPLAY_CODES = {
    "unassigned": 0,
    "headlight": 1, "bell": 13, "horn": 14, "whistle": 15, "pantograph": 11,
    "smoke": 10, "engine": 4, "light": 74, "coupler clank": 28, "couple": 122,
    "uncouple": 9,
    "shunting mode": 7, "momentum": 8,
    "brake": 57, "brake release": 200, "dynamic brake": 41,
    "manual notch down": 31, "manual notch up": 30, "reverser": 69, "mute": 100,
    "far light": 12, "cab light": 3, "ditch lights": 48, "step lights": 98,
    "tail lights": 62, "switching lights": 58, "dimmer": 51, "interior lights": 2,
    "air compressor": 42, "air pump": 45, "injector": 60, "exhaust fan": 108,
    "radiator fan": 17, "steam generator": 66, "blower": 105, "blow down": 56,
    "safety": 38, "sanding": 55, "ash dump": 88, "shoveling": 18, "water fill": 35,
    "long whistle": 103, "short whistle": 64, "doppler horn": 63,
    "curve squeal": 36, "brake squeal": 21, "announce": 6, "cab chatter": 27,
}

NAME_SCHEMES = ("id", "road")


# ------------------------------------------------------------ pure helpers
# Everything in this section runs under Jython 2.7 and CPython 3 and is
# covered by tests/test_cs105_roster_sync.py.

def display_code(label):
    """CS-105 Display code for a roster function label (0 = plain text)."""
    if label is None:
        return 0
    return DISPLAY_CODES.get(label.strip().lower(), 0)


def _dotted(byte_values):
    return ".".join("%02X" % b for b in byte_values)


def train_node_id(address, is_long):
    """Dotted node ID of the CS-105 virtual train node for a DCC address."""
    address = int(address)
    if is_long:
        hi = 0xC0 | ((address >> 8) & 0x3F)
    else:
        hi = 0
    return "%s.%02X.%02X" % (TRAIN_NODE_PREFIX, hi, address & 0xFF)


def search_event(address, is_long, allocate=True):
    """Train Search event ID (dotted) for a DCC address.

    Digits are packed as 6 BCD nibbles, left aligned and padded with 0xF.
    Flags: Exact + Address-only + DCC, plus Allocate and Force-long as asked.
    """
    digits = str(int(address))
    if not 1 <= len(digits) <= 6:
        raise ValueError("address out of range: %r" % (address,))
    nibbles = [int(d) for d in digits] + [0xF] * (6 - len(digits))
    query = [(nibbles[i] << 4) | nibbles[i + 1] for i in (0, 2, 4)]
    flags = FLAG_EXACT | FLAG_ADDRESS_ONLY | PROTO_DCC
    if allocate:
        flags |= FLAG_ALLOCATE
    if is_long:
        flags |= DCC_LONG
    return _dotted(list(SEARCH_PREFIX) + query + [flags])


def is_train_node(node_id):
    return str(node_id).upper().startswith(TRAIN_NODE_PREFIX + ".")


def is_cs105_model(model_name):
    return CS105_MODEL in (model_name or "")


def load_entry(path, index_attrs=None):
    """One roster entry from its locomotive file as a plain dict."""
    root = ET.parse(path).getroot()
    loco = root.find("locomotive")
    if loco is None:
        raise ValueError("no <locomotive> in %s" % path)
    attrs = dict(index_attrs or {})
    attrs.update(loco.attrib)
    address = int(attrs.get("dccAddress") or 0)
    dcc = loco.find("locoaddress/dcclocoaddress")
    if dcc is not None and dcc.get("longaddress") is not None:
        is_long = dcc.get("longaddress") == "yes"
    else:
        is_long = address > 127
    labels = {}
    for fl in loco.iter("functionlabel"):
        try:
            num = int(fl.get("num"))
        except (TypeError, ValueError):
            continue
        labels[num] = ((fl.text or "").strip(), fl.get("lockable", "true") == "true")
    return {
        "id": attrs.get("id") or "",
        "address": address,
        "long": is_long,
        "comment": attrs.get("comment") or "",
        "road_name": (attrs.get("roadName") or "").strip(),
        "road_number": (attrs.get("roadNumber") or "").strip(),
        "labels": labels,
        "file": os.path.basename(path),
    }


def load_roster(index_path, files_dir):
    """All entries listed in roster.xml, read from their locomotive files."""
    root = ET.parse(str(index_path)).getroot()
    entries = []
    for loco in root.iter("locomotive"):
        file_name = loco.get("fileName")
        if not file_name:
            continue
        entries.append(load_entry(os.path.join(str(files_dir), file_name), loco.attrib))
    return entries


def train_name(entry, scheme="id"):
    """CS-105 Train.Name for an entry: the roster ID, or "<road> <number>"."""
    if scheme == "road":
        parts = [entry.get("road_name") or "", entry.get("road_number") or ""]
        name = " ".join(p for p in parts if p)
        if name:
            return name
    elif scheme != "id":
        raise ValueError("unknown name scheme %r" % (scheme,))
    return entry["id"]


def desired_values(entry, include_name, names="id"):
    """CDI key -> value to write for one entry (TcsUploadAction's rules).

    An empty roster comment leaves Train.User Description as it is, so a
    description typed on a throttle is not wiped by an empty roster field.
    """
    values = {}
    if include_name:
        values["Train.Name"] = train_name(entry, names)
    if entry.get("comment"):
        values["Train.User Description"] = entry["comment"]
    for i in range(N_FUNCTIONS):
        label, lockable = entry["labels"].get(i + 1, ("", True))
        code = display_code(label)
        values["Train.Functions(%d).Display" % i] = code
        values["Train.Functions(%d).Momentary" % i] = 0 if lockable else 1
        if code == 0:
            values["Train.Functions(%d).Description" % i] = label
    return values


def plan(entries, existing_node_ids, rename=False, only=None, names="id"):
    """Decide create/update per entry against the node IDs the CS-105 has."""
    existing = set(str(n).upper() for n in existing_node_ids)
    actions = []
    for entry in entries:
        if only and entry["id"] not in only:
            continue
        if not entry["address"]:
            actions.append({"entry": entry, "action": "skip", "node": None,
                            "values": {}, "reason": "no DCC address"})
            continue
        node = train_node_id(entry["address"], entry["long"])
        create = node not in existing
        actions.append({
            "entry": entry,
            "action": "create" if create else "update",
            "node": node,
            "event": search_event(entry["address"], entry["long"]) if create else None,
            "values": desired_values(entry, include_name=create or rename, names=names),
        })
    return actions


def unknown_ids(entries, only):
    """IDs asked for with Only that are not in the roster (likely typos)."""
    if not only:
        return []
    known = set(e["id"] for e in entries)
    return sorted(i for i in only if i not in known)


def unmanaged_train_nodes(existing_node_ids, entries):
    """CS-105 train nodes with no roster entry at all (reported, never touched)."""
    rostered = set(train_node_id(e["address"], e["long"]) for e in entries if e["address"])
    return sorted(n for n in existing_node_ids if is_train_node(n) and n.upper() not in rostered)


def summary(actions):
    counts = {"create": 0, "update": 0, "skip": 0}
    for a in actions:
        counts[a["action"]] += 1
    return counts


def describe(action):
    e = action["entry"]
    kind = "long" if e["long"] else "short"
    labelled = sum(1 for i in range(N_FUNCTIONS) if e["labels"].get(i + 1, ("", True))[0])
    name = action["values"].get("Train.Name")
    return "%-6s %-10s addr %4d %-5s node %s  %2d labels%s%s" % (
        action["action"].upper(), e["id"], e["address"], kind, action["node"] or "-",
        labelled, "  name -> %r" % name if name is not None else "",
        "  (%s)" % action["reason"] if action.get("reason") else "")


def parse_only(value):
    if not value:
        return None
    if isinstance(value, (list, tuple, set)):
        return set(str(v) for v in value) or None
    return set(p.strip() for p in str(value).split(",") if p.strip()) or None


def pick_connection(connections, wanted=None):
    """Choose one LCC connection from (prefix, user name) pairs.

    Returns its index. With one connection, that one. With several, `wanted`
    must match a prefix or user name, otherwise ValueError names the choices.
    """
    if not connections:
        raise ValueError("no LCC (OpenLCB) connection in this JMRI")
    if wanted:
        for i, (prefix, name) in enumerate(connections):
            if wanted in (prefix, name):
                return i
        raise ValueError("no LCC connection called %r; have: %s" % (
            wanted, ", ".join("%s (%s)" % c for c in connections)))
    if len(connections) == 1:
        return 0
    raise ValueError("JMRI has %d LCC connections; choose one: %s" % (
        len(connections), ", ".join("%s (%s)" % c for c in connections)))


def options_from(globs):
    """Read and remove the CS105_* options from a globals dict.

    They are removed so that a later run from the Scripting menu, which shares
    JMRI's interpreter, does not silently inherit CS105_APPLY=True.
    Returns None when no option was set (interactive run).
    """
    if not any(n in globs for n in OPTION_NAMES):
        return None
    opts = {
        "apply": bool(globs.get("CS105_APPLY", False)),
        "only": globs.get("CS105_ONLY"),
        "rename": bool(globs.get("CS105_RENAME", False)),
        "names": str(globs.get("CS105_NAMES") or "id"),
        "connection": globs.get("CS105_CONNECTION") or None,
    }
    for n in OPTION_NAMES:
        globs.pop(n, None)
    if opts["names"] not in NAME_SCHEMES:
        raise ValueError("CS105_NAMES must be one of %s" % ", ".join(NAME_SCHEMES))
    return opts


# ---------------------------------------------------------------- JMRI side

def _jmri_available():
    """True only inside JMRI's Jython."""
    if not sys.platform.startswith("java"):
        return False
    try:
        import jmri
        return hasattr(jmri, "InstanceManager")
    except ImportError:
        return False


def _wait_until(predicate, timeout, step=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def _lcc_connections():
    """[(memo, iface)] for every JMRI connection that carries OpenLCB."""
    import jmri
    from org.openlcb import OlcbInterface
    found = []
    for memo in jmri.InstanceManager.getList(jmri.jmrix.can.CanSystemConnectionMemo):
        try:
            iface = memo.get(OlcbInterface)
        except Exception:
            iface = None
        if iface is not None:
            found.append((memo, iface))
    return found


def _connection_labels(conns):
    return [(m.getSystemPrefix(), m.getUserName()) for m, _ in conns]


class BusState(object):
    """What the chosen LCC connection can see right now."""

    def __init__(self, memo, iface):
        self.memo = memo
        self.iface = iface
        self.store = iface.getNodeStore()
        self.nodes = []
        self.cs105 = None

    def refresh(self, log, wait=4.0):
        """Ask every node to announce itself, then look for the CS-105 by its SNIP model."""
        from org.openlcb import NodeID
        self.store.refresh()
        time.sleep(wait)
        self.nodes = [str(nm.getNodeID()) for nm in self.store.getNodeMemos()]
        others = [n for n in self.nodes if not is_train_node(n)]
        for n in others:
            self.store.getSimpleNodeIdent(NodeID(n))  # asks for SNIP

        def model(n):
            ident = self.store.getSimpleNodeIdent(NodeID(n))
            return ident.getModelName() if ident is not None else ""

        def found():
            for n in others:
                if is_cs105_model(model(n)):
                    self.cs105 = n
                    return True
            return False

        self.cs105 = None
        _wait_until(found, 6.0)
        log("LCC connection %s (%s): %d nodes, %d train nodes, CS-105 %s" % (
            self.memo.getSystemPrefix(), self.memo.getUserName(), len(self.nodes),
            sum(1 for n in self.nodes if is_train_node(n)),
            self.cs105 or "NOT FOUND"))

    def train_user_name(self, node):
        from org.openlcb import NodeID
        ident = self.store.getSimpleNodeIdent(NodeID(node))
        return ident.getUserName() if ident is not None else ""


def _load_jmri_roster():
    import jmri
    roster = jmri.jmrit.roster.Roster.getDefault()
    return load_roster(roster.getRosterIndexPath(), roster.getRosterFilesLocation())


def _plan_lines(bus, entries, actions, only, log):
    for i in unknown_ids(entries, only):
        log("WARNING: %r is not a roster ID; check the spelling" % i)
    for a in actions:
        log(describe(a))
    for n in unmanaged_train_nodes(bus.nodes, entries):
        log("KEEP   train node %s (%s) has no roster entry; untouched" % (n, bus.train_user_name(n)))
    c = summary(actions)
    log("Plan: %d to create, %d to update, %d skipped." % (c["create"], c["update"], c["skip"]))
    if c["update"]:
        log("Updating rewrites every function label and, where the roster has a comment, the train description.")


def _write_values(iface, node_dotted, values, log, timeout=60.0):
    """Load a train node's CDI, write differing values, wait for completion."""
    from java.beans import PropertyChangeListener
    from org.openlcb import NodeID
    from org.openlcb.cdi.impl import ConfigRepresentation

    state = {"loaded": False, "pending": 0}

    class _Loaded(PropertyChangeListener):
        def propertyChange(self, ev):
            if ev.getPropertyName() == ConfigRepresentation.UPDATE_CACHE_COMPLETE:
                state["loaded"] = True

    class _Written(PropertyChangeListener):
        def propertyChange(self, ev):
            if ev.getPropertyName() == ConfigRepresentation.UPDATE_WRITE_COMPLETE:
                state["pending"] -= 1

    rep = ConfigRepresentation(iface, NodeID(node_dotted))
    rep.addPropertyChangeListener(_Loaded())
    if not _wait_until(lambda: state["loaded"], timeout):
        log("  settings did not load for %s: %s" % (node_dotted, rep.getStatus()))
        return None
    written = _Written()
    changed = 0
    missing = []
    for key in sorted(values):
        entry = rep.getVariableForKey(key)
        if entry is None:
            missing.append(key)
            continue
        want = values[key]
        if isinstance(entry, ConfigRepresentation.StringEntry):
            if (entry.getValue() or "") == want:
                continue
            entry.addPropertyChangeListener(written)
            state["pending"] += 1
            entry.setValue(want)
        elif isinstance(entry, ConfigRepresentation.IntegerEntry):
            if int(entry.getValue()) == int(want):
                continue
            entry.addPropertyChangeListener(written)
            state["pending"] += 1
            entry.setValue(long(want))  # noqa: F821  (Jython 2.7)
        else:
            missing.append(key + " (unexpected type)")
            continue
        changed += 1
    if missing:
        log("  settings not found on %s: %s" % (node_dotted, ", ".join(missing)))
    if not _wait_until(lambda: state["pending"] <= 0, timeout):
        log("  %d write(s) still unconfirmed on %s" % (state["pending"], node_dotted))
    return changed


def _apply(bus, actions, log):
    """Create and update trains. Refuses without a CS-105 or during another run."""
    from java.lang import System
    from org.openlcb import NodeID, EventID, IdentifyProducersMessage

    if bus.cs105 is None:
        raise RuntimeError("no CS-105 answered on this LCC connection; nothing written. "
                           "Check it is powered and connected, then show the plan again.")
    if System.getProperty(LOCK_PROPERTY):
        raise RuntimeError("another CS-105 roster sync is still running; nothing written")
    System.setProperty(LOCK_PROPERTY, "1")
    try:
        iface, store = bus.iface, bus.store
        created = updated = failed = 0
        for a in actions:
            if a["action"] == "skip":
                continue
            node = a["node"]
            if a["action"] == "create":
                log("creating %s on the CS-105 (Train Search %s)" % (node, a["event"]))
                iface.getOutputConnection().put(
                    IdentifyProducersMessage(iface.getNodeId(), EventID(a["event"])), None)
                if not _wait_until(lambda: store.findNode(NodeID(node)) is not None, 15.0):
                    log("  the CS-105 did not announce %s; skipping %s" % (node, a["entry"]["id"]))
                    failed += 1
                    continue
                created += 1
            changed = _write_values(iface, node, a["values"], log)
            if changed is None:
                failed += 1
                continue
            if a["action"] == "update":
                updated += 1
            log("  %s: %d field(s) written" % (a["entry"]["id"], changed))
        log("Done: %d created, %d updated, %d failed." % (created, updated, failed))
        return failed
    finally:
        System.clearProperty(LOCK_PROPERTY)


def _choose_bus(wanted):
    conns = _lcc_connections()
    memo, iface = conns[pick_connection(_connection_labels(conns), wanted)]
    return BusState(memo, iface)


def run_scripted(opts):
    """No window: plan, and apply when CS105_APPLY=True. Returns the log text."""
    lines = []

    def log(msg):
        lines.append(msg)
        print(msg)

    only = parse_only(opts["only"])
    bus = _choose_bus(opts["connection"])
    log("CS-105 roster sync %s: %s" % (VERSION, "APPLY" if opts["apply"] else "PLAN ONLY"))
    bus.refresh(log)
    entries = _load_jmri_roster()
    actions = plan(entries, bus.nodes, rename=opts["rename"], only=only, names=opts["names"])
    _plan_lines(bus, entries, actions, only, log)
    if bus.cs105 is None:
        log("No CS-105 answered on this connection, so applying is refused until it does.")
    if not opts["apply"]:
        log("Nothing written. Set CS105_APPLY=True to write.")
        return "\n".join(lines)
    if _apply(bus, actions, log):
        raise RuntimeError("\n".join(lines + ["some trains failed; see above"]))
    return "\n".join(lines)


def run_window():
    """Scripting-menu run: a window that shows the plan and applies on request."""
    import threading
    from java.awt import BorderLayout, FlowLayout, Font
    from java.util import Vector
    from javax.swing import (JButton, JCheckBox, JComboBox, JFrame, JLabel, JOptionPane,
                             JPanel, JScrollPane, JTextArea, JTextField, SwingUtilities)

    conns = _lcc_connections()
    labels = _connection_labels(conns)
    if not conns:
        JOptionPane.showMessageDialog(None, "This JMRI has no LCC (OpenLCB) connection.\n"
                                      "Add one in Preferences > Connections first.",
                                      "CS-105 Roster Sync", JOptionPane.WARNING_MESSAGE)
        return

    frame = JFrame("CS-105 Roster Sync %s" % VERSION)
    conn_box = JComboBox(Vector(["%s (%s)" % c for c in labels]))
    only_field = JTextField(18)
    only_field.setToolTipText("Roster IDs separated by commas; empty = whole roster")
    rename_box = JCheckBox("Rename trains that already exist")
    names_box = JComboBox(Vector(["Roster ID", "Road name + number"]))
    text = JTextArea(26, 100)
    text.setEditable(False)
    text.setFont(Font("Monospaced", Font.PLAIN, 12))
    plan_btn = JButton("Show plan")
    apply_btn = JButton("Apply...")
    close_btn = JButton("Close")
    apply_btn.setEnabled(False)
    state = {"bus": None, "actions": None, "busy": False}

    def log(msg):
        SwingUtilities.invokeLater(lambda: text.append(msg + "\n"))

    def settings():
        return (parse_only(only_field.getText()), rename_box.isSelected(),
                "road" if names_box.getSelectedIndex() == 1 else "id")

    def busy(flag):
        state["busy"] = flag

        def ui():
            plan_btn.setEnabled(not flag)
            apply_btn.setEnabled((not flag) and state["actions"] is not None
                                 and state["bus"] is not None and state["bus"].cs105 is not None)
        SwingUtilities.invokeLater(ui)

    def in_background(work):
        def body():
            busy(True)
            try:
                work()
            except Exception as e:
                log("ERROR: %s" % e)
            finally:
                busy(False)
        t = threading.Thread(target=body)
        t.setDaemon(True)
        t.start()

    def show_plan(_event=None):
        state["actions"] = None
        text.setText("")
        only, rename, names = settings()
        memo, iface = conns[conn_box.getSelectedIndex()]

        def work():
            bus = BusState(memo, iface)
            bus.refresh(log)
            entries = _load_jmri_roster()
            actions = plan(entries, bus.nodes, rename=rename, only=only, names=names)
            _plan_lines(bus, entries, actions, only, log)
            if bus.cs105 is None:
                log("No CS-105 answered, so Apply is off. Check it is powered and on this "
                    "LCC connection, then press Show plan again.")
            else:
                log("Nothing written yet. Press Apply to write this plan.")
            state["bus"], state["actions"] = bus, actions
        in_background(work)

    def apply(_event=None):
        actions, bus = state["actions"], state["bus"]
        if not actions or bus is None:
            return
        c = summary(actions)
        msg = ("Write to the CS-105 now?\n\n%d train(s) to create, %d to update.\n\n"
               "Function labels are replaced from the roster. A train's description is replaced "
               "where the roster entry has a comment.\nNo train moves and track power is "
               "not touched.") % (c["create"], c["update"])
        if JOptionPane.showConfirmDialog(frame, msg, "Apply CS-105 roster sync",
                                         JOptionPane.OK_CANCEL_OPTION) != JOptionPane.OK_OPTION:
            return
        state["actions"] = None  # one apply per plan

        def work():
            log("")
            _apply(bus, actions, log)
            log("Press Show plan to check the result.")
        in_background(work)

    plan_btn.addActionListener(show_plan)
    apply_btn.addActionListener(apply)
    close_btn.addActionListener(lambda e: frame.dispose())

    top = JPanel(FlowLayout(FlowLayout.LEFT))
    if len(conns) > 1:
        top.add(JLabel("LCC connection:"))
        top.add(conn_box)
    top.add(JLabel("Only roster IDs:"))
    top.add(only_field)
    top.add(JLabel("Train names:"))
    top.add(names_box)
    top.add(rename_box)
    buttons = JPanel(FlowLayout(FlowLayout.RIGHT))
    for b in (plan_btn, apply_btn, close_btn):
        buttons.add(b)
    frame.getContentPane().add(top, BorderLayout.NORTH)
    frame.getContentPane().add(JScrollPane(text), BorderLayout.CENTER)
    frame.getContentPane().add(buttons, BorderLayout.SOUTH)
    frame.pack()
    frame.setLocationRelativeTo(None)
    frame.setVisible(True)
    show_plan()


# ---------------------------------------------------------------- CLI side

def _cli(argv):
    import argparse
    p = argparse.ArgumentParser(description="Plan a CS-105 roster sync from roster files (no device access).")
    p.add_argument("roster_dir", help="folder holding roster.xml and roster/")
    p.add_argument("--only", help="comma-separated roster IDs")
    p.add_argument("--existing", help="comma-separated train node IDs the CS-105 already has")
    p.add_argument("--rename", action="store_true", help="plan Train.Name for existing trains too")
    p.add_argument("--names", choices=NAME_SCHEMES, default="id", help="train name scheme")
    p.add_argument("--values", action="store_true", help="also print the values per entry")
    args = p.parse_args(argv)
    entries = load_roster(os.path.join(args.roster_dir, "roster.xml"),
                          os.path.join(args.roster_dir, "roster"))
    existing = parse_only(args.existing) or set()
    only = parse_only(args.only)
    for i in unknown_ids(entries, only):
        print("WARNING: %r is not a roster ID" % i)
    actions = plan(entries, existing, rename=args.rename, only=only, names=args.names)
    for a in actions:
        print(describe(a))
        if args.values:
            for k in sorted(a["values"]):
                print("    %s = %r" % (k, a["values"][k]))
    return 0


if _jmri_available():
    _opts = options_from(globals())
    if _opts is not None:
        RESULT = run_scripted(_opts)
    else:
        from java.awt import GraphicsEnvironment
        if GraphicsEnvironment.isHeadless():
            RESULT = run_scripted({"apply": False, "only": None, "rename": False,
                                   "names": "id", "connection": None})
        else:
            RESULT = "window opened"
            run_window()
elif __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
