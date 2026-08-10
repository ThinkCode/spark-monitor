#!/usr/bin/env python3
"""Authorisation for the endpoints that can change the cluster.

Everything else this project serves is read-only. Switching a topology stops
and starts models, so it needs a different bar, and the threat model on a
homelab LAN is not "nobody can reach it".

THE DESIGN, AND WHY

1.  OFF BY DEFAULT. Control does nothing until you set `control.enabled` and a
    token. Someone who upgrades and does not read the release notes gets the
    same read-only server they had before.

2.  A SHARED TOKEN, SENT IN A CUSTOM HEADER. Not a login form, and not HTTP
    Basic. The header matters as much as the token:

    This dashboard has no authentication and typically runs on a LAN, so any
    web page you visit while logged into your own network can POST to it --
    a browser will happily submit a cross-origin form to
    http://your-dashboard:8088/api/topology/switch, and you would never see
    it happen. That is CSRF, and a password in a form field does not stop it,
    because the browser attaches the credentials for you.

    A custom header cannot be set by a cross-origin form at all. It forces a
    CORS preflight, which this server answers for nobody. So requiring
    `X-Spark-Token` is both the authentication and the CSRF defence, and the
    second is arguably the more important of the two on a home network.

3.  A NETWORK ALLOWLIST AS WELL. Defence in depth: the token protects against
    a browser being tricked; the allowlist protects against a device on the
    network guessing or capturing one.

4.  CONSTANT-TIME COMPARISON. Token checks use hmac.compare_digest so a
    timing side-channel cannot be used to recover the token byte by byte.

WHAT THIS IS NOT

    It is not encryption. Over plain HTTP the token crosses the network in
    clear text, and anyone able to capture your traffic can read it. If you
    want control from outside a network you fully trust, put it on a
    WireGuard-based VPN (Tailscale is the easy option here) or reach it
    through an SSH tunnel. Both encrypt the whole conversation, and the
    default allowlist is loopback plus the Tailscale range for exactly that
    reason. See docs/TOPOLOGY.md.
"""
import hmac
import ipaddress
import os

HEADER = "X-Spark-Token"

# Loopback and the Tailscale CGNAT range. Deliberately NOT the whole RFC1918
# space: "anything on my LAN" includes the smart TV and whatever your guests
# brought with them.
DEFAULT_NETWORKS = ["127.0.0.0/8", "::1/128", "100.64.0.0/10"]


def config(cfg):
    """Normalise the `control` block, and fail closed on a bad one."""
    c = dict(cfg.get("control") or {})
    token = c.get("token") or os.environ.get("SPARK_MONITOR_TOKEN") or ""
    nets = c.get("networks") or DEFAULT_NETWORKS
    enabled = bool(c.get("enabled"))
    problems = []
    if enabled and not token:
        # Refuse rather than silently serving control endpoints unauthenticated.
        problems.append(
            "control.enabled is set but no token is configured; control stays "
            "OFF. Set control.token or the SPARK_MONITOR_TOKEN environment "
            "variable.")
        enabled = False
    if enabled and len(token) < 16:
        problems.append(
            "control.token is shorter than 16 characters; control stays OFF. "
            "Generate one with: python3 -c \"import secrets;"
            "print(secrets.token_urlsafe(32))\"")
        enabled = False
    parsed = []
    for n in nets:
        try:
            parsed.append(ipaddress.ip_network(n, strict=False))
        except ValueError:
            problems.append("ignoring unparseable control network %r" % n)
    return {"enabled": enabled, "token": token, "networks": parsed,
            "network_names": [str(n) for n in parsed], "problems": problems}


def network_ok(ctl, ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in ctl["networks"])


def token_ok(ctl, supplied):
    if not ctl["token"]:
        return False
    return hmac.compare_digest(str(supplied or ""), ctl["token"])


def check(ctl, ip, supplied):
    """(allowed, http_status, message). Messages say which check failed: this
    runs on your own network, and a control panel that refuses without saying
    why wastes an evening."""
    if not ctl["enabled"]:
        return False, 404, ("control endpoints are disabled; set control.enabled "
                            "and control.token to use them")
    if not network_ok(ctl, ip):
        return False, 403, ("%s is not in control.networks (%s)"
                            % (ip, ", ".join(ctl["network_names"])))
    if not token_ok(ctl, supplied):
        return False, 401, "missing or incorrect %s header" % HEADER
    return True, 200, "ok"
