"""TradingView daily bars for continuous futures (evidence-only data plumbing).

Fetches daily OHLCV for continuous front-month futures via TradingView's
public websocket data protocol (data.tradingview.com), unauthenticated.
Delayed data is fine here: daily HTF levels do not need real-time quotes.

The websocket client is stdlib-only (socket/ssl) so it behaves identically
with or without an egress proxy and always sends the Origin header
TradingView's edge requires. Bars are cached locally as JSON with a daily
refresh cadence. The engine tick path NEVER touches the network --
htf_levels reads the cache only via daily_bars_for_scan(). A manual refresh:
    python -m compass.tradingview_daily --symbols MNQ.c.0,MES.c.0,MGC.v.0,SIL.v.0,MCL.v.0
(Scheduling a refresh is deferred to Phase 2.)

Evidence only. No alerts, no trades, no credentials. Every public entry
point degrades to an empty result on network/protocol failure, never an
exception.
"""
import base64
import hashlib
import json
import os
import random
import re
import socket
import ssl
import struct
import time
from pathlib import Path
from urllib.parse import urlparse

TV_WS_HOST = "data.tradingview.com"
TV_WS_PATH = "/socket.io/websocket"
TV_WS_URL = "wss://%s%s" % (TV_WS_HOST, TV_WS_PATH)
ORIGIN = "https://www.tradingview.com"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
AUTH_TOKEN = "unauthorized_user_token"  # public delayed data, no login
RESOLUTION = "D"                        # daily bars
N_BARS_DEFAULT = 365                    # ~1 year lookback
CACHE_MAX_AGE_S = 24 * 3600             # refresh daily
CONNECT_TIMEOUT_S = 15
SYMBOL_TIMEOUT_S = 90
REFRESH_PAUSE_S = 2.0                   # pacing between symbols

# ICT alias root -> TradingView continuous front-month future.
TV_SYMBOLS = {
    "MNQ": "CME_MINI:MNQ1!", "NQ": "CME_MINI:NQ1!",
    "MES": "CME_MINI:MES1!", "ES": "CME_MINI:ES1!",
    "MGC": "COMEX:MGC1!", "GC": "COMEX:GC1!",
    "SIL": "COMEX:SIL1!", "SI": "COMEX:SI1!",
    "MCL": "NYMEX:MCL1!", "CL": "NYMEX:CL1!",
}


def tv_symbol_for(alias):
    """Map an ICT alias like 'MNQ.c.0' to a TradingView symbol, or None."""
    root = (alias or "").split(".")[0].strip().upper()
    return TV_SYMBOLS.get(root)


# ---------------------------------------------------------------------------
# TradingView wire protocol: messages framed as ~m~<len>~m~<payload>.
# Server pings look like ~m~4~m~~h~1 and must be echoed back verbatim.
# ---------------------------------------------------------------------------

_FRAME_RE = re.compile(r"~m~(\d+)~m~")


def frame_messages(messages):
    """Encode one or more dict/str messages into a single wire string."""
    parts = []
    for m in messages:
        payload = m if isinstance(m, str) else json.dumps(m, separators=(",", ":"))
        parts.append("~m~%d~m~%s" % (len(payload), payload))
    return "".join(parts)


def split_frames(buf):
    """Split a wire buffer into (payloads, remainder). Incomplete tail kept."""
    payloads = []
    pos = 0
    while True:
        m = _FRAME_RE.match(buf, pos)
        if not m:
            break
        n = int(m.group(1))
        start = m.end()
        if len(buf) < start + n:
            break
        payloads.append(buf[start:start + n])
        pos = start + n
    return payloads, buf[pos:]


def is_ping(payload):
    return payload.startswith("~h~")


# ---------------------------------------------------------------------------
# Minimal RFC 6455 client (stdlib only): proxy-aware, sends Origin header.
# ---------------------------------------------------------------------------

def _proxy_for_https():
    for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    return None


class _WSClient:
    """Blocking websocket text client. recv_text() returns str or None."""

    def __init__(self, timeout=CONNECT_TIMEOUT_S):
        self.timeout = timeout
        self.sock = None
        self._frag = []

    def connect(self):
        proxy = _proxy_for_https()
        if proxy:
            pu = urlparse(proxy)
            s = socket.create_connection((pu.hostname, pu.port or 8080),
                                         timeout=self.timeout)
            auth = ""
            if pu.username:
                creds = "%s:%s" % (pu.username, pu.password or "")
                auth = "Proxy-Authorization: Basic %s\r\n" % base64.b64encode(
                    creds.encode()).decode()
            s.sendall(("CONNECT %s:443 HTTP/1.1\r\nHost: %s:443\r\n%s\r\n"
                       % (TV_WS_HOST, TV_WS_HOST, auth)).encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = s.recv(4096)
                if not chunk:
                    raise OSError("proxy CONNECT failed")
                resp += chunk
            if b" 200 " not in resp.split(b"\r\n", 1)[0]:
                raise OSError("proxy refused CONNECT: %s"
                              % resp.split(b"\r\n", 1)[0][:80])
            ctx = ssl.create_default_context()
            s = ctx.wrap_socket(s, server_hostname=TV_WS_HOST)
        else:
            ctx = ssl.create_default_context()
            s = ctx.wrap_socket(
                socket.create_connection((TV_WS_HOST, 443), timeout=self.timeout),
                server_hostname=TV_WS_HOST)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\nOrigin: %s\r\nUser-Agent: %s\r\n\r\n"
            % (TV_WS_PATH, TV_WS_HOST, key, ORIGIN, USER_AGENT))
        s.sendall(req.encode())
        s.settimeout(self.timeout)
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                raise OSError("handshake: no response")
            resp += chunk
        status = resp.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise OSError("handshake rejected: %s" % status[:120])
        s.settimeout(None)
        self.sock = s

    def _send_frame(self, opcode, data=b""):
        mask = os.urandom(4)
        n = len(data)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack("!Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(hdr + mask + masked)

    def send_text(self, data):
        raw = data.encode("utf-8") if isinstance(data, str) else data
        self._send_frame(0x1, raw)

    def _recv_exact(self, n, deadline):
        chunks = []
        while n > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("recv timeout")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(n)
            if not chunk:
                raise OSError("connection closed")
            chunks.append(chunk)
            n -= len(chunk)
        return b"".join(chunks)

    def recv_text(self, timeout):
        """Next complete text message, or None on timeout/close. Never raises."""
        deadline = time.monotonic() + timeout
        try:
            while True:
                hdr = self._recv_exact(2, deadline)
                b1, b2 = hdr[0], hdr[1]
                opcode = b1 & 0x0F
                masked = bool(b2 & 0x80)
                length = b2 & 0x7F
                if length == 126:
                    length = struct.unpack("!H", self._recv_exact(2, deadline))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._recv_exact(8, deadline))[0]
                mask = self._recv_exact(4, deadline) if masked else None
                payload = self._recv_exact(length, deadline) if length else b""
                if mask:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                if opcode == 0x8:      # close
                    return None
                if opcode == 0x9:      # ping -> pong
                    self._send_frame(0xA, payload)
                    continue
                if opcode == 0xA:      # pong
                    continue
                if opcode == 0x1:
                    self._frag = []
                    return payload.decode("utf-8", "ignore")
                if opcode == 0x0:      # continuation
                    self._frag.append(payload)
                    if b1 & 0x80:
                        msg = b"".join(self._frag)
                        self._frag = []
                        return msg.decode("utf-8", "ignore")
        except Exception:
            return None

    def close(self):
        try:
            if self.sock:
                try:
                    self._send_frame(0x8, b"")
                except Exception:
                    pass
                self.sock.close()
        except Exception:
            pass
        finally:
            self.sock = None


# ---------------------------------------------------------------------------
# Bar parsing: timescale_update / du payloads carry either array-style
# {"t":[],"o":[],"h":[],"l":[],"c":[],"v":[]} or row-style {"s":[{"i":n,"v":[]}]}.
# ---------------------------------------------------------------------------

def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


def parse_series_payload(data):
    """Extract ascending [{ts,o,h,l,c,v}] from a timescale_update/du dict."""
    bars = {}
    if not isinstance(data, dict):
        return []
    for _key, series in data.items():
        if not isinstance(series, dict):
            continue
        t = series.get("t")
        if isinstance(t, list) and t:
            o = series.get("o") or []
            h = series.get("h") or []
            l = series.get("l") or []
            c = series.get("c") or []
            v = series.get("v") or []
            for i, ts in enumerate(t):
                ts = _num(ts)
                oo = _num(o[i]) if i < len(o) else None
                hh = _num(h[i]) if i < len(h) else None
                ll = _num(l[i]) if i < len(l) else None
                cc = _num(c[i]) if i < len(c) else None
                if ts is None or None in (oo, hh, ll, cc):
                    continue
                vv = _num(v[i]) if i < len(v) else None
                bars[ts] = {"ts": ts, "o": oo, "h": hh, "l": ll, "c": cc, "v": vv}
            continue
        s = series.get("s")
        if isinstance(s, list):
            for row in s:
                v = row.get("v") if isinstance(row, dict) else None
                if not v or len(v) < 5:
                    continue
                ts = _num(v[0])
                oo, hh, ll, cc = _num(v[1]), _num(v[2]), _num(v[3]), _num(v[4])
                if ts is None or None in (oo, hh, ll, cc):
                    continue
                vv = _num(v[5]) if len(v) > 5 else None
                bars[ts] = {"ts": ts, "o": oo, "h": hh, "l": ll, "c": cc, "v": vv}
    return [bars[k] for k in sorted(bars)]


# ---------------------------------------------------------------------------
# Live fetch. Sync; call only from sync contexts (CLI, manual refresh).
# ---------------------------------------------------------------------------

def _session_id():
    return "cs_" + "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=12))


def _fetch_tv_bars(tv_symbol, n_bars=N_BARS_DEFAULT, timeout=SYMBOL_TIMEOUT_S):
    client = _WSClient()
    acc = {}
    try:
        client.connect()
    except Exception:
        return []
    try:
        sess = _session_id()
        sym_id = "sds_sym_1"
        symbol_def = "=" + json.dumps({"symbol": tv_symbol,
                                       "adjustment": "splits"})
        client.send_text(frame_messages([
            {"m": "set_auth_token", "p": [AUTH_TOKEN]},
            {"m": "chart_create_session", "p": [sess, ""]},
            {"m": "resolve_symbol", "p": [sess, sym_id, symbol_def]},
            {"m": "create_series", "p": [sess, "sds_1", "s1", sym_id,
                                        RESOLUTION, n_bars]},
        ]))
        buf = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = client.recv_text(timeout=max(1.0, deadline - time.monotonic()))
            if raw is None:
                break
            buf += raw
            payloads, buf = split_frames(buf)
            done = False
            for p in payloads:
                if is_ping(p):
                    try:
                        client.send_text(frame_messages([p]))
                    except Exception:
                        pass
                    continue
                try:
                    msg = json.loads(p)
                except ValueError:
                    continue
                mtype = msg.get("m")
                params = msg.get("p") or []
                if mtype in ("timescale_update", "du") and len(params) > 1:
                    for b in parse_series_payload(params[1]):
                        acc[b["ts"]] = b
                elif mtype in ("series_completed", "series_error", "symbol_error"):
                    done = True
                    break
            if done:
                break
    finally:
        client.close()
    return [acc[k] for k in sorted(acc)]


def fetch_daily_bars(alias, n_bars=N_BARS_DEFAULT, timeout=SYMBOL_TIMEOUT_S):
    """Fetch daily bars for one ICT alias. Never raises.

    Returns {'ok', 'bars', 'reason', 'tv_symbol'}; bars are ascending
    [{ts,o,h,l,c,v}].
    """
    tv_symbol = tv_symbol_for(alias)
    if not tv_symbol:
        return {"ok": False, "bars": [], "reason": "unmapped_symbol",
                "tv_symbol": None}
    try:
        bars = _fetch_tv_bars(tv_symbol, n_bars, timeout)
    except Exception:
        bars = []
    if not bars:
        return {"ok": False, "bars": [], "reason": "no_bars",
                "tv_symbol": tv_symbol}
    return {"ok": True, "bars": bars, "reason": None, "tv_symbol": tv_symbol}


# ---------------------------------------------------------------------------
# Local cache: JSON per alias, refreshed daily. Scan path reads cache only.
# ---------------------------------------------------------------------------

def default_cache_dir():
    override = (os.environ.get("ICT_TV_CACHE_DIR") or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".cache" / "market-compass" / "tradingview_daily"


def cache_file(alias, cache_dir=None):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", alias or "unknown")
    base = Path(cache_dir) if cache_dir else default_cache_dir()
    return base / (safe + ".json")


def save_cached_bars(alias, bars, tv_symbol, cache_dir=None):
    path = cache_file(alias, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "alias": alias, "tv_symbol": tv_symbol,
        "fetched_at": time.time(), "bars": bars,
    }))
    tmp.replace(path)
    return str(path)


def load_cached_bars(alias, cache_dir=None):
    """Return cached ascending [{ts,o,h,l,c}] or []. Never raises."""
    try:
        payload = json.loads(cache_file(alias, cache_dir).read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    out = []
    for b in payload.get("bars") or []:
        if not isinstance(b, dict):
            continue
        if None in (b.get("ts"), b.get("o"), b.get("h"), b.get("l"), b.get("c")):
            continue
        out.append({"ts": b["ts"], "o": b["o"], "h": b["h"],
                    "l": b["l"], "c": b["c"]})
    return sorted(out, key=lambda b: b["ts"])


def cache_fresh(alias, cache_dir=None, max_age_s=CACHE_MAX_AGE_S):
    """True when a usable cache file exists and is younger than max_age_s."""
    try:
        payload = json.loads(cache_file(alias, cache_dir).read_text())
        fetched_at = float((payload or {}).get("fetched_at", 0))
        bars = (payload or {}).get("bars") or []
    except (OSError, ValueError, TypeError):
        return False
    return bool(bars) and (time.time() - fetched_at) < max_age_s


def daily_bars_for_scan(alias, cache_dir=None):
    """Cache-only read for the htf_levels scan path. Never touches network."""
    try:
        return load_cached_bars(alias, cache_dir=cache_dir)
    except Exception:
        return []


def refresh(symbols, cache_dir=None, max_age_s=CACHE_MAX_AGE_S, force=False,
            n_bars=N_BARS_DEFAULT, pause_s=REFRESH_PAUSE_S):
    """Fetch stale/missing symbols only. Returns {alias: result-dict}."""
    results = {}
    todo = [s for s in (symbols or []) if s]
    for i, alias in enumerate(todo):
        if not force and cache_fresh(alias, cache_dir, max_age_s):
            results[alias] = {"ok": True, "cached": True, "bars": None,
                              "reason": None,
                              "tv_symbol": tv_symbol_for(alias)}
            continue
        if i:
            time.sleep(pause_s)
        res = fetch_daily_bars(alias, n_bars=n_bars)
        results[alias] = dict(res, cached=False,
                              bars=None if res["ok"] else [])
        if res["ok"]:
            save_cached_bars(alias, res["bars"], res["tv_symbol"], cache_dir)
    return results


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Refresh cached TradingView daily bars for ICT futures.")
    ap.add_argument("--symbols", default="MNQ.c.0,MES.c.0,MGC.v.0,SIL.v.0,MCL.v.0",
                    help="comma-separated ICT aliases")
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--force", action="store_true",
                    help="refetch even when cache is fresh")
    ap.add_argument("--n-bars", type=int, default=N_BARS_DEFAULT)
    args = ap.parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    results = refresh(symbols, cache_dir=args.cache_dir, force=args.force,
                      n_bars=args.n_bars)
    for alias, r in results.items():
        if r.get("cached"):
            print("%s: cache fresh, skipped" % alias)
        elif r["ok"]:
            print("%s: ok (%s)" % (alias, r["tv_symbol"]))
        else:
            print("%s: FAILED reason=%s" % (alias, r["reason"]))
    failed = [a for a, r in results.items() if not r["ok"]]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
