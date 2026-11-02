"""Setting up by hand: checks in plain words, heights from what people measure, drafts and go-live,
the audit trail, retire, and bundles that round-trip."""
import json

import pytest
from helpers import ACTOR, CSV_SOURCE, basic_network, make_env

from waterworks_desk import logchain, model


def problems_of(fn, *a, **k):
    with pytest.raises(model.SetupError) as e:
        fn(*a, **k)
    return " | ".join(e.value.errors)


def test_basic_network_and_heights():
    tmp, cfg, conn = make_env(CSV_SOURCE)
    basic_network(conn)
    s = conn.execute("SELECT * FROM sites WHERE key='ps1'").fetchone()
    assert abs(s["lat"] - (45 + 46 / 60 + 37 / 3600)) < 1e-6 and abs(s["lon"] - (7 + 42 / 60 + 3 / 3600)) < 1e-6
    assert s["ground_level_m"] == 2.5
    assert s["floor_level_m"] == pytest.approx(-1.5)          # 4.0 m below a 2.5 m ground
    assert s["overflow_level_m"] == pytest.approx(2.0)        # 3.5 m above the floor
    wl = conn.execute("SELECT * FROM sensors WHERE key='ps1-wl'").fetchone()
    assert wl["mount_level_m"] == pytest.approx(-1.3)         # 0.2 m above the floor
    assert json.loads(wl["mount_entry_json"]) == {"ref": "above_floor", "value": 0.2}   # as typed, kept
    dp = conn.execute("SELECT * FROM sensors WHERE key='ps1-p1-dp'").fetchone()
    assert dp["unit"] == "m" and dp["source_unit"] == "bar"
    assert dp["warn_lo"] == pytest.approx(2.0 * 10.1972, rel=1e-4)   # limits typed in bar, stored as metres head
    assert s["status"] == "draft" and dp["status"] == "draft"


def test_checks_in_plain_words():
    tmp, cfg, conn = make_env(CSV_SOURCE)
    basic_network(conn)
    known = {"exports"}
    # outside the region: probably swapped coordinates
    msg = problems_of(model.save, conn, "site", {"key": "x", "system": "dw", "name": "X", "site_type": "tank",
                                                 "lat": 107.7, "lon": 45.77}, ACTOR)
    assert "latitude must be between" in msg
    msg = problems_of(model.save, conn, "site", {"key": "x", "system": "dw", "name": "X", "site_type": "tank",
                                                 "lat": 46.9, "lon": 9.1}, ACTOR)          # 160 km away
    assert "outside its 60 km area" in msg
    # a depth can't be negative
    msg = problems_of(model.save, conn, "site", {"key": "x", "system": "dw", "name": "X", "site_type": "wet_well",
                                                 "lat": 45.78, "lon": 7.70, "ground_level_m": 3,
                                                 "floor_level_m": {"ref": "below_ground", "value": -2}}, ACTOR)
    assert "can't be negative" in msg and "above ground" in msg
    # floor/overflow order
    msg = problems_of(model.save, conn, "site", {"key": "x", "system": "dw", "name": "X", "site_type": "wet_well",
                                                 "lat": 45.78, "lon": 7.70, "floor_level_m": 3, "overflow_level_m": 1}, ACTOR)
    assert "must be above the floor" in msg
    # measured from something the site doesn't have
    model.save(conn, "site", {"key": "gauge", "system": "dw", "name": "Canal gauge", "site_type": "canal_gauge",
                              "lat": 45.79, "lon": 7.69}, ACTOR)
    msg = problems_of(model.save, conn, "sensor", {"key": "s", "site": "gauge", "name": "S", "quantity": "level",
                                                   "source": "manual", "mount_level_m": {"ref": "above_floor", "value": 1}}, ACTOR)
    assert "the site has no floor level yet. Add it to the site first" in msg
    # units that don't match the quantity, limits out of order, unknown source, missing tag
    msg = problems_of(model.save, conn, "sensor", {"key": "s2", "site": "ps1", "name": "S2", "quantity": "flow",
                                                   "source": "scada", "source_unit": "bar",
                                                   "warn_hi": 5, "alarm_hi": 4}, ACTOR, known)
    assert "isn't a unit of flow" in msg and "no data source 'scada'" in msg
    assert "say which tag" in msg and "must be below the alarm hi" in msg
    # the same tag twice
    msg = problems_of(model.save, conn, "sensor", {"key": "dup", "site": "ps1", "name": "Dup", "quantity": "flow",
                                                   "source": "exports", "source_tag": "PS1_FT"}, ACTOR, known)
    assert "already reads tag 'PS1_FT'" in msg
    # a component at another site
    model.save(conn, "site", {"key": "t1", "system": "dw", "name": "Hill tank", "site_type": "tank",
                              "lat": 45.80, "lon": 7.72}, ACTOR)
    msg = problems_of(model.save, conn, "sensor", {"key": "s3", "site": "t1", "component": "ps1-p1", "name": "S3",
                                                   "quantity": "level", "source": "manual"}, ACTOR, known)
    assert "at a different site" in msg
    # every change needs a name
    msg = problems_of(model.save, conn, "site", {"key": "t1", "name": "Renamed"}, "  ")
    assert "a name is required" in msg
    with pytest.raises(ValueError):
        logchain.clean_actor("")


def test_warnings_dont_block():
    tmp, cfg, conn = make_env(CSV_SOURCE)
    basic_network(conn)
    res = model.save(conn, "site", {"key": "near", "system": "dw", "name": "Next door", "site_type": "valve_house",
                                    "lat": 45.776944, "lon": 7.700833}, ACTOR)
    assert any("isn't a site type" in w for w in res["warnings"])
    assert any("within 2 m of Riverside" in w for w in res["warnings"])


def test_go_live_needs_parents_and_checks():
    tmp, cfg, conn = make_env(CSV_SOURCE)
    basic_network(conn)
    known = {"exports"}
    msg = problems_of(model.go_live, conn, "site", "ps1", ACTOR, known)
    assert "system is still a draft" in msg
    model.save(conn, "sensor", {"key": "nosrc", "site": "ps1", "name": "Unfinished sensor", "quantity": "level"}, ACTOR, known)
    msg = problems_of(model.go_live, conn, "system", "dw", ACTOR, known)
    assert "Unfinished sensor (nosrc): say where its readings come from" in msg
    assert conn.execute("SELECT COUNT(*) FROM sensors WHERE status='live'").fetchone()[0] == 0   # all or nothing
    model.save(conn, "sensor", {"key": "nosrc", "source": "manual"}, ACTOR, known)
    res = model.go_live(conn, "system", "dw", ACTOR, known)
    assert res["live"] == 1 + 1 + 2 + 4     # system, site, 2 components, 4 sensors
    assert conn.execute("SELECT COUNT(*) FROM sensors WHERE status!='live'").fetchone()[0] == 0


def test_audit_trail_names_every_change_and_verifies():
    tmp, cfg, conn = make_env(CSV_SOURCE)
    basic_network(conn)
    model.save(conn, "site", {"key": "ps1", "name": "Riverside PS (renamed)"}, "Lan Pham")
    model.retire(conn, "component", "ps1-p2", "Lan Pham", "replaced")
    log = logchain.entries(conn)
    last = log[-2]
    assert last["actor"] == "Lan Pham" and last["data"]["before"] == {"name": "Riverside pumping station"}
    assert last["data"]["after"] == {"name": "Riverside PS (renamed)"}
    assert log[-1]["data"]["action"] == "retire" and "replaced" in log[-1]["msg"]
    assert logchain.verify(conn)["ok"]
    conn.execute("UPDATE log SET msg='nothing happened' WHERE seq=3")
    conn.commit()
    v = logchain.verify(conn)
    assert not v["ok"] and v["problems"][0]["problem"] == "content changed after signing"
    # retired things can't be edited back silently
    msg = problems_of(model.save, conn, "component", {"key": "ps1-p2", "name": "Pump 2 again"}, ACTOR)
    assert "was retired" in msg


def test_bundle_round_trip_and_all_or_nothing():
    tmp, cfg, conn = make_env(CSV_SOURCE)
    basic_network(conn)
    b = model.export_bundle(conn)
    assert [k for k in b] == ["regions", "systems", "sites", "components", "sensors"]
    tmp2, cfg2, conn2 = make_env(CSV_SOURCE)
    res = model.import_bundle(conn2, b, ACTOR, {"exports"})
    assert res["counts"] == {"region": 1, "system": 1, "site": 1, "component": 2, "sensor": 3}
    for t in ("sites", "sensors"):
        a = [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY key")]
        c = [dict(r) for r in conn2.execute(f"SELECT * FROM {t} ORDER BY key")]
        strip = lambda rows: [{k: (round(v, 9) if isinstance(v, float) else v) for k, v in r.items()
                               if k not in ("id", "created_ms", "updated_ms", "system_id", "site_id", "component_id")} for r in rows]
        assert strip(a) == strip(c)
    # one bad item: nothing from the bundle is saved
    b2 = {"sites": [{"key": "ok1", "system": "dw", "name": "Fine", "site_type": "tank", "lat": 45.79, "lon": 7.71},
                    {"key": "bad", "system": "dw", "name": "Bad", "site_type": "tank", "lat": 99, "lon": 7.71}]}
    with pytest.raises(model.SetupError) as e:
        model.import_bundle(conn2, b2, ACTOR, {"exports"})
    assert "site bad: latitude must be between" in e.value.errors[0]
    assert conn2.execute("SELECT 1 FROM sites WHERE key='ok1'").fetchone() is None


def test_malformed_setup_files_are_refused_in_words():
    tmp, cfg, conn = make_env(CSV_SOURCE)
    basic_network(conn)
    for bad, words in (({"sites": "notalist"}, '"sites" should be a list'), ({"sites": [1]}, "should be an object"),
                       ({"layers": [{"key": "l", "system": "dw", "name": "L", "geojson": {"type": "FeatureCollection", "features": [
                           {"type": "Feature", "geometry": {"type": "Point", "coordinates": "x"}}]}}]}, "must be GeoJSON"),
                       ({"layers": [{"key": "l", "system": "dw", "name": "L", "geojson": {"type": "FeatureCollection", "features": [
                           {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[200, 51], [0, 51]]}}]}}]}, "outside the world")):
        with pytest.raises(model.SetupError) as e:
            model.import_bundle(conn, bad, ACTOR, {"exports"})
        assert words in " ".join(e.value.errors), e.value.errors
