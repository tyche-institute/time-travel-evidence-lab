#!/usr/bin/env python3
"""
EU/EEA-wide action-time vs review-time status census over the real published
member-state Trusted Lists reachable from the EU LOTL (ETSI TS 119 612).

Generalises experiment 08 from one list (Estonia) to the whole EU trust
infrastructure. "Drift" = a service whose CURRENT status is revoked-class but
whose OWN published ServiceHistory records an earlier VALID status, so an action
performed in the valid window reads as revoked under a current-only review yet
valid under dated replay.

VALID is the full ETSI active vocabulary, not just `granted`: older / other
member states use `accredited` / `undersupervision` / `recognisedatnationallevel`
for the pre-eIDAS or nationally-recognised valid period. Counting only `granted`
silently undercounts those lists (e.g. Czechia's services transition
accredited -> withdrawn), so the census uses the union.

Unit of analysis: one TSPService entry per the list's own granularity. Entry
counts are NOT distinct-provider counts (some lists enumerate per certificate).
Nothing here is synthetic: every status value and dated StatusStartingTime is
read directly from the deposited list files.
"""
import argparse, datetime as dt, glob, hashlib, json, os
import xml.etree.ElementTree as ET

NS = "http://uri.etsi.org/02231/v2#"
VALID = {"granted", "accredited", "undersupervision", "recognisedatnationallevel", "setbynationallaw"}
REVOKED = {"withdrawn", "deprecatedatnationallevel", "deprecatedbynationallaw",
           "supervisionceased", "supervisionrevoked", "accreditationceased",
           "accreditationrevoked", "setbynationallaw_revoked"}

EU27 = {"AT","BE","BG","HR","CY","CZ","DK","EE","FI","FR","DE","EL","HU","IE",
        "IT","LV","LT","LU","MT","NL","PL","PT","RO","SK","SI","ES","SE"}
EEA = {"IS","LI","NO"}


def _short(u): return u.rsplit("/", 1)[-1].lower() if u else u
def _dtp(s): return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _sdi(tsp):
    for dv in tsp.iter(f"{{{NS}}}DigitalId"):
        c = dv.find(f"{{{NS}}}X509Certificate")
        if c is not None and c.text:
            return hashlib.sha256(c.text.encode()).hexdigest()[:12]
    return "no-sdi"


def load(tl_path):
    root = ET.parse(tl_path).getroot()
    out = []
    for tsp in root.iter(f"{{{NS}}}TSPService"):
        info = tsp.find(f"{{{NS}}}ServiceInformation")
        if info is None:
            continue
        name = info.find(f".//{{{NS}}}ServiceName/{{{NS}}}Name")
        cur = info.find(f"{{{NS}}}ServiceStatus")
        cst = info.find(f"{{{NS}}}StatusStartingTime")
        if name is None or cur is None or cst is None:
            continue
        events = [(_short(cur.text), _dtp(cst.text))]
        for hi in tsp.iter(f"{{{NS}}}ServiceHistoryInstance"):
            st = hi.find(f"{{{NS}}}ServiceStatus"); tt = hi.find(f"{{{NS}}}StatusStartingTime")
            if st is not None and tt is not None:
                events.append((_short(st.text), _dtp(tt.text)))
        events = sorted(set(events), key=lambda e: (e[1], e[0]))
        out.append({"name": name.text, "sdi": _sdi(tsp),
                    "current": _short(cur.text), "current_time": _dtp(cst.text), "events": events})
    return out


def last_valid_window(events):
    start = end = None
    for i, (s, t) in enumerate(events):
        if s in VALID:
            start = t
            end = events[i + 1][1] if i + 1 < len(events) else None
    return None if start is None else (start, end)


def status_at(events, when):
    ap = [e for e in events if e[1] <= when]
    return ap[-1][0] if ap else None


def midpoint(start, end):
    if end is None:
        end = start + dt.timedelta(days=365)
    return start + (end - start) / 2


def has_drift(inst):
    return inst["current"] in REVOKED and any(e[0] in VALID for e in inst["events"])


def operational(inst):
    w = last_valid_window(inst["events"])
    if w is None:
        return False
    return True if w[1] is None else (w[1] - w[0]) >= dt.timedelta(days=30)


def verdict(cur, rep):
    if rep is None:
        return "not_yet_listed_at_action_time"
    if cur == rep:
        return "valid_both_times" if rep in VALID else "consistent_non_valid"
    if rep in VALID and cur in REVOKED:
        return "valid_when_performed_revoked_at_review"
    return "status_changed"


def census_one(tl_path):
    insts = load(tl_path)
    sha = hashlib.sha256(open(tl_path, "rb").read()).hexdigest()
    drift = [i for i in insts if has_drift(i)]
    oper = [i for i in drift if operational(i)]
    return {"path": tl_path, "sha256": sha, "total": len(insts),
            "drift": len(drift), "operational": len(oper),
            "_insts": insts, "_drift_oper": oper}


def worked_examples(insts, oper):
    rows, seen = [], set()
    for inst in sorted(oper, key=lambda i: i["current_time"]):
        if inst["name"] in seen:
            continue
        w = last_valid_window(inst["events"])
        if w is None:
            continue
        a = midpoint(*w)
        rows.append({"role": "drift_example", "service": inst["name"], "action_time": a.isoformat(),
                     "current_only": inst["current"], "dated_replay": status_at(inst["events"], a),
                     "verdict": verdict(inst["current"], status_at(inst["events"], a))})
        seen.add(inst["name"])
        if len(rows) >= 3:
            break
    stable = next((i for i in insts if i["current"] in VALID and all(e[0] in VALID for e in i["events"])), None)
    if stable:
        w = last_valid_window(stable["events"]); a = midpoint(*w)
        rows.append({"role": "control_no_drift", "service": stable["name"], "action_time": a.isoformat(),
                     "current_only": stable["current"], "dated_replay": status_at(stable["events"], a),
                     "verdict": verdict(stable["current"], status_at(stable["events"], a))})
    if oper:
        head = sorted(oper, key=lambda i: i["current_time"])[0]
        before = min(e[1] for e in head["events"]) - dt.timedelta(days=3650)
        rows.append({"role": "control_before_listing", "service": head["name"], "action_time": before.isoformat(),
                     "current_only": head["current"], "dated_replay": status_at(head["events"], before) or "—",
                     "verdict": verdict(head["current"], status_at(head["events"], before))})
    return rows


def territory(path):
    return os.path.basename(path)[3:5].upper()


def run_all(tl_glob, detail="EE"):
    lists = []
    for path in sorted(glob.glob(tl_glob)):
        t = territory(path)
        c = census_one(path)
        grp = "EU" if t in EU27 else ("EEA" if t in EEA else "other")
        row = {"territory": t, "group": grp, "total": c["total"], "drift": c["drift"],
               "operational": c["operational"], "sha256": c["sha256"],
               "drift_pct": round(100 * c["drift"] / c["total"], 1) if c["total"] else 0.0}
        if t == detail:
            row["worked_examples"] = worked_examples(c["_insts"], c["_drift_oper"])
        lists.append(row)
    eu = [r for r in lists if r["group"] == "EU"]
    eea = [r for r in lists if r["group"] == "EEA"]

    def agg(g):
        return {"lists": len(g), "total": sum(r["total"] for r in g),
                "drift": sum(r["drift"] for r in g), "operational": sum(r["operational"] for r in g)}
    return {"lists": lists, "eu": agg(eu), "eea": agg(eea),
            "eu_lists_with_drift": sum(1 for r in eu if r["drift"] > 0)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="data/public-corpus/raw/national-tls/tl-*.xml")
    ap.add_argument("--detail", default="EE")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    out = run_all(a.glob, a.detail)
    if a.json:
        print(json.dumps(out, indent=1, default=str))
    else:
        eu = out["eu"]
        print(f"EU-{eu['lists']}: {eu['total']} entries, {eu['drift']} valid->revoked drift "
              f"({100*eu['drift']/eu['total']:.1f}%), {eu['operational']} operational(>=30d); "
              f"{out['eu_lists_with_drift']}/{eu['lists']} lists positive")
        for r in sorted(out["lists"], key=lambda r: -r["total"]):
            print(f"  {r['territory']:3} {r['group']:4} {r['total']:5d} {r['drift']:5d} {r['drift_pct']:5.1f}%")
