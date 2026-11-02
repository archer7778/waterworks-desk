"""Phone alerts through ntfy. Critical alerts are sent loud, warnings quiet; advisories only go in
the shift summary. The public ntfy.sh is for demos: a real deployment points ntfy_server at its
own ntfy, so site and alert status never pass through a third party.

(Escalation to the next person when nobody acknowledges arrives in M2 with named accounts.)
"""
import urllib.request

from . import __version__

PRIORITY = {"critical": ("urgent", "rotating_light"), "warning": ("default", "warning")}


def _ascii(s):
    return s.encode("latin-1", "replace").decode("latin-1")


def send(cfg, title, message, priority="default", tags=""):
    n = cfg["notify"]
    topic = str(n.get("ntfy_topic") or "").strip()
    if not topic:
        return False
    headers = {"Title": _ascii(title), "Priority": priority, "Tags": tags, "User-Agent": f"WaterworksDesk/{__version__}"}
    if n.get("console_url"):
        headers["Click"] = n["console_url"]
    req = urllib.request.Request(n["ntfy_server"].rstrip("/") + "/" + topic, data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception:
        return False


def alerts_changed(cfg, changes, log=print):
    """Push new and worsened critical and warning alerts."""
    sent = 0
    for a in changes.get("opened", []) + changes.get("escalated", []):
        if a["severity"] not in PRIORITY:
            continue
        prio, tag = PRIORITY[a["severity"]]
        if send(cfg, f"{a['severity'].capitalize()}: Waterworks Desk", a["message"], prio, tag):
            sent += 1
    if sent and log:
        log(f"Sent {sent} phone alert(s).")
    return sent
