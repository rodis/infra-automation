#!/usr/bin/env python3
"""Make Dagu's Telegram notification routing match the playbook. Run ON the watcher, as rods_pinky,
by playbooks/watcher/install.yml:

  python3 notifications.py '<spec json>'      -> prints "changed" or "unchanged"

spec: {"template_channel": "<name of the existing channel to copy the bot from>",
       "events": [...], "workspaces": {"<workspace>": {"channel": "<name>", "topic": "<id>"}}}

Why this exists (2026-10-09): the owner deliberately has no access to Dagu's UI and does not manage it,
so the notification settings — a channel per Telegram topic, a route per workspace — are managed here
like the rest of the watcher. Dagu keeps them as JSON files: channels/<sha256(channel id)>.json and
routes/workspaces/<sha256(workspace)>.json (worked out from the files on 2026-10-09, Dagu 2.17.2).

THE BOT TOKEN IS NEVER HANDLED. It is stored encrypted with this Dagu install's key (botTokenEnc); a new
channel for the same bot copies that encrypted value from the template channel, on the box. Nothing
here decrypts, prints or transmits it.

A channel's id is derived from its name (uuid5), so a re-run finds the same file and changes nothing.
"""
import datetime, glob, hashlib, json, os, sys, uuid

D = os.path.expanduser("~/.local/share/dagu/data/notifications")
NS = uuid.UUID("6f1d3a52-6b0e-4f7e-9d7a-6c2f0a9c1e77")   # fixed namespace for this repo's channel ids
spec = json.loads(sys.argv[1])
now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
sha = lambda s: hashlib.sha256(s.encode()).hexdigest()
changed = False


def load(path):
    with open(path) as f:
        return json.load(f)


def write_if_different(path, doc, compare_keys):
    """Write doc unless the file already holds the same values for compare_keys (timestamps ignored)."""
    global changed
    if os.path.exists(path):
        cur = load(path)
        if all(cur.get(k) == doc.get(k) for k in compare_keys):
            return
        doc["createdAt"] = cur.get("createdAt", now)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    changed = True


channels = {load(f)["name"]: load(f) for f in glob.glob(f"{D}/channels/*.json")}
template = channels.get(spec["template_channel"])
if not template or template.get("type") != "telegram" or not template["telegram"].get("botTokenEnc"):
    sys.exit(f"STOP: template channel {spec['template_channel']!r} missing or not a Telegram channel with a token")

for ws, want in spec["workspaces"].items():
    name, topic = want["channel"], str(want["topic"])
    ch = channels.get(name)
    if ch is None:   # a new channel: same bot and chat, its own topic
        ch = {"id": str(uuid.uuid5(NS, name)), "name": name, "type": "telegram", "enabled": True,
              "createdAt": now, "updatedBy": template.get("updatedBy")}
    ch = {**ch, "enabled": True, "updatedAt": now,
          "telegram": {**template["telegram"], **ch.get("telegram", {}), "topicId": topic,
                       "botTokenEnc": template["telegram"]["botTokenEnc"], "chatId": template["telegram"]["chatId"]}}
    write_if_different(f"{D}/channels/{sha(ch['id'])}.json", ch, ["name", "enabled", "telegram"])
    channels[name] = ch

    rpath = f"{D}/routes/workspaces/{sha(ws)}.json"
    cur = load(rpath) if os.path.exists(rpath) else {}
    route = {"id": cur.get("id", str(uuid.uuid5(NS, "route:" + ws))), "scope": "workspace", "workspace": ws,
             "enabled": True, "inheritGlobal": False,
             "routes": [{"id": (cur.get("routes") or [{}])[0].get("id", str(uuid.uuid5(NS, "route-entry:" + ws))),
                         "channelId": ch["id"], "enabled": True, "events": spec["events"]}],
             "createdAt": cur.get("createdAt", now), "updatedAt": now, "updatedBy": template.get("updatedBy")}
    write_if_different(rpath, route, ["enabled", "inheritGlobal", "routes"])

print("changed" if changed else "unchanged")
