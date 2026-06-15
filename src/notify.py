#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pluggable notifications. Sends a message to each configured sink.

Sink types (in config "notify" list):
  {"type": "command", "cmd": ["/path/script", "--message", "{msg}"]}
      -> runs the command; "{msg}" placeholders are replaced. If no placeholder
         is present, the message is piped to stdin instead.
  {"type": "webhook", "url": "...", "method": "POST",
   "json_field": "text", "headers": {"Authorization": "Bearer ..."}}
      -> json_field set: POST JSON {<field>: msg}; omit it: POST raw text body.

Module: notify.send(sinks, msg)   CLI: notify.py <config.json> "<message>"
"""
import json, subprocess, sys, urllib.request


def _command(sink, msg):
    has_ph = any("{msg}" in a for a in sink["cmd"])
    cmd = [a.replace("{msg}", msg) for a in sink["cmd"]]
    if has_ph:
        subprocess.run(cmd, timeout=sink.get("timeout", 120))
    else:
        subprocess.run(cmd, input=msg.encode("utf-8"), timeout=sink.get("timeout", 120))


def _webhook(sink, msg):
    headers = dict(sink.get("headers", {}))
    if sink.get("json_field"):
        body = json.dumps({sink["json_field"]: msg}, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    else:
        body = msg.encode("utf-8")
        headers.setdefault("Content-Type", "text/plain; charset=utf-8")
    req = urllib.request.Request(sink["url"], data=body, headers=headers,
                                 method=sink.get("method", "POST"))
    urllib.request.urlopen(req, timeout=sink.get("timeout", 30)).read()


def send(sinks, msg, log=print):
    for sink in (sinks or []):
        t = sink.get("type")
        try:
            if t == "command":
                _command(sink, msg)
            elif t == "webhook":
                _webhook(sink, msg)
            else:
                log("notify: unknown sink type %r" % t)
        except Exception as e:
            log("notify %s failed: %s" % (t, e))


if __name__ == "__main__":
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
    send(cfg.get("notify", []), sys.argv[2])
