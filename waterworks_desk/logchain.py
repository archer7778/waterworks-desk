"""The signed audit trail. The server and the setup tools are the only writers, so entries form a
single chain: each one stores the SHA-256 of its own content plus the previous entry's hash.
Editing or deleting an entry breaks the chain from that point.

Every entry names its actor: the person who acknowledged, shelved or changed something, or
'system'. Until named accounts arrive (M2) the actor is the name typed with the passcode; the
field is in the chain from the start so adding accounts never means re-signing old entries.
"""
import hashlib
import json
import threading

from .db import now_ms

KINDS = {
    "ack", "shelve", "unshelve", "note", "handover", "check",      # control-room habits, as in Downlink Desk
    "setup",                                                         # anything created, changed or retired by hand
    "go_live", "workorder", "component", "override", "import", "system",
}
GENESIS = "genesis"
_lock = threading.Lock()


def canon(seq, t_ms, kind, data_json, msg, actor, prev):
    return "␟".join([prev, str(seq), str(t_ms), kind, data_json, msg, actor])


def digest(*parts):
    return hashlib.sha256(canon(*parts).encode("utf-8")).hexdigest()


def clean_actor(actor):
    actor = " ".join(str(actor or "").split())[:60]
    if not actor:
        raise ValueError("a name is required for every change, so the audit trail says who made it")
    return actor


def append(conn, kind, data, msg, actor, commit=True):
    if kind not in KINDS:
        raise ValueError("unknown entry kind")
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    msg = str(msg).strip()[:1200]
    actor = clean_actor(actor)
    if not msg:
        raise ValueError("a message is required")
    data_json = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    if len(data_json) > 16000:
        raise ValueError("entry data too large")
    with _lock:
        last = conn.execute("SELECT seq, hash FROM log ORDER BY seq DESC LIMIT 1").fetchone()
        seq = (last["seq"] + 1) if last else 1
        prev = last["hash"] if last else GENESIS
        t = now_ms()
        h = digest(seq, t, kind, data_json, msg, actor, prev)
        conn.execute("INSERT INTO log(seq,t_ms,kind,data_json,msg,actor,prev,hash) VALUES(?,?,?,?,?,?,?,?)",
                     (seq, t, kind, data_json, msg, actor, prev, h))
        if commit:
            conn.commit()
    return row_to_entry({"seq": seq, "t_ms": t, "kind": kind, "data_json": data_json,
                         "msg": msg, "actor": actor, "prev": prev, "hash": h})


def row_to_entry(r):
    return {"seq": r["seq"], "t": r["t_ms"], "kind": r["kind"], "data": json.loads(r["data_json"]),
            "msg": r["msg"], "actor": r["actor"], "prev": r["prev"], "hash": r["hash"]}


def entries(conn, after=0, limit=5000):
    rows = conn.execute("SELECT * FROM log WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)).fetchall()
    return [row_to_entry(r) for r in rows]


def verify(conn):
    prev, expected_seq, problems, n = GENESIS, 1, [], 0
    for r in conn.execute("SELECT * FROM log ORDER BY seq"):
        n += 1
        if r["seq"] != expected_seq:
            problems.append({"seq": r["seq"], "problem": f"entries {expected_seq}-{r['seq'] - 1} are missing"})
        if r["prev"] != prev:
            problems.append({"seq": r["seq"], "problem": "does not link to the entry before it"})
        h = digest(r["seq"], r["t_ms"], r["kind"], r["data_json"], r["msg"], r["actor"], r["prev"])
        if h != r["hash"]:
            problems.append({"seq": r["seq"], "problem": "content changed after signing"})
        prev, expected_seq = r["hash"], r["seq"] + 1
    return {"ok": not problems, "entries": n, "head": prev, "problems": problems[:20]}
