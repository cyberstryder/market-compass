"""Dark pool flow via TraderMatrix MCP server.

The REST API families don't expose dark pool prints, but the MCP server
(POST /api/v1/mcp, same X-API-Key) lists dark-pool flow among its 32 tools.
This module discovers the tool at runtime via tools/list, then calls it.

Design:
- scan() runs on its own throttle, stores darkpool:latest in the DB.
- Flow Pulse reads the stored data for confirmation (no blocking on tick).
- Evidence only: confirmation is context on pulses, never a standalone trigger.

MCP is JSON-RPC 2.0 over POST. A bare tools/call is accepted (no initialize
handshake), one POST per call.
"""
import time

MCP_URL = "https://api.traderdaddy.pro/api/v1/mcp"
VERSION = "darkpool-v1"
SCAN_THROTTLE = 300  # 5 minutes; dark pool prints are not high-frequency
CONFIRM_WINDOW = 86400  # 24h lookback for confirmation
MIN_NOTIONAL = 500000  # $500k+ block to count as institutional


def _rpc(method, params=None, rpc_id=1):
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _headers(api_key):
    return {"X-API-Key": api_key, "Content-Type": "application/json",
            "Accept": "application/json"}


def discover_tools(api_key, timeout=15):
    """Call tools/list, return the raw tool list. Raises on transport error."""
    import httpx
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(MCP_URL, headers=_headers(api_key),
                           json=_rpc("tools/list"))
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError("MCP tools/list error: %s" % (data["error"],))
    return (data.get("result") or {}).get("tools") or []


def find_darkpool_tool(tools):
    """Pick the dark-pool flow tool from a tools/list result. None if absent."""
    candidates = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "")
        desc = str(t.get("description") or "")
        blob = (name + " " + desc).lower()
        if "dark" in blob and ("pool" in blob or "print" in blob or "flow" in blob):
            candidates.append(t)
    if not candidates:
        return None
    # Prefer a name with both 'dark' and 'pool'.
    for t in candidates:
        if "dark" in t.get("name", "").lower() and "pool" in t.get("name", "").lower():
            return t
    return candidates[0]


def call_tool(api_key, tool_name, arguments=None, timeout=30):
    """Call one MCP tool via tools/call. Returns the result payload."""
    import httpx
    params = {"name": tool_name, "arguments": arguments or {}}
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(MCP_URL, headers=_headers(api_key),
                           json=_rpc("tools/call", params))
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError("MCP tools/call error: %s" % (data["error"],))
    return data.get("result")


def _extract_prints(result):
    """Normalize a tools/call result into a list of print dicts.

    MCP tools return content blocks; be liberal in what we accept.
    """
    if result is None:
        return []
    # Direct list
    if isinstance(result, list):
        items = result
    elif isinstance(result, dict):
        # MCP content envelope
        content = result.get("content")
        if isinstance(content, list):
            items = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    import json as _json
                    try:
                        parsed = _json.loads(block.get("text") or "")
                        if isinstance(parsed, list):
                            items.extend(parsed)
                        elif isinstance(parsed, dict):
                            # Maybe {"prints": [...]} or {"data": [...]}
                            for key in ("prints", "data", "results", "trades"):
                                if isinstance(parsed.get(key), list):
                                    items.extend(parsed[key])
                                    break
                            else:
                                items.append(parsed)
                    except Exception:
                        continue
        else:
            items = []
            for key in ("prints", "data", "results", "trades"):
                if isinstance(result.get(key), list):
                    items = result[key]
                    break
    else:
        items = []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        out.append(it)
    return out


def _normalize_print(raw):
    """Best-effort normalization of one dark pool print."""
    def num(*keys):
        for k in keys:
            v = raw.get(k)
            try:
                if v is not None:
                    return float(v)
            except (TypeError, ValueError):
                continue
        return None
    symbol = str(raw.get("symbol") or raw.get("ticker") or "").upper()
    # Direction: explicit field, else infer from price vs vwap/prev close if present.
    direction = str(raw.get("direction") or raw.get("side") or "").lower()
    if direction not in ("bullish", "bearish", "buy", "sell", "b", "s"):
        direction = None
    elif direction in ("buy", "b"):
        direction = "bullish"
    elif direction in ("sell", "s"):
        direction = "bearish"
    return {
        "symbol": symbol,
        "direction": direction,
        "notional": num("notional", "dollar_volume", "value", "premium", "size_usd"),
        "shares": num("shares", "volume", "size"),
        "price": num("price", "execution_price"),
        "ts": num("ts", "timestamp", "time", "executed_at"),
        "raw": {k: v for k, v in raw.items()
                if k not in ("symbol", "ticker", "direction", "side")},
    }


def scan(db, c, cfg, now):
    """Throttled pass: discover tool once, fetch dark pool prints, store."""
    if not getattr(cfg, 'darkpool', True):
        return {'ran': False, 'reason': 'disabled'}
    api_key = getattr(cfg, 'matrix', '') or ''
    if not api_key:
        return {'ran': False, 'reason': 'no_api_key'}
    if now - db.get(c, 'darkpool:scanned_at', 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    try:
        tool_name = db.get(c, 'darkpool:tool_name', '')
        if not tool_name:
            tools = discover_tools(api_key)
            tool = find_darkpool_tool(tools)
            if tool is None:
                db.put(c, 'darkpool:scanned_at', now)
                db.put(c, 'darkpool:latest',
                       {'at': now, 'status': 'no_tool',
                        'reason': 'no dark-pool tool in MCP tools/list',
                        'tool_count': len(tools)})
                return {'ran': True, 'status': 'no_tool'}
            tool_name = tool.get("name")
            db.put(c, 'darkpool:tool_name', tool_name)
            db.put(c, 'darkpool:tool_schema', tool.get("inputSchema") or {})
        result = call_tool(api_key, tool_name, {})
        prints = [_normalize_print(r) for r in _extract_prints(result)]
        prints = [p for p in prints if p["symbol"]]
        symbols = {}
        for p in prints:
            symbols.setdefault(p["symbol"], []).append(p)
        db.put(c, 'darkpool:scanned_at', now)
        db.put(c, 'darkpool:latest',
               {'at': now, 'status': 'ok', 'tool': tool_name,
                'print_count': len(prints),
                'symbols': sorted(symbols),
                'prints': prints[:200]})
        db.health('darkpool', 'available',
                  '%d prints via %s' % (len(prints), tool_name), poll_ts=now)
        return {'ran': True, 'prints': len(prints), 'tool': tool_name}
    except Exception as e:
        db.put(c, 'darkpool:scanned_at', now)
        db.health('darkpool', 'error', 'MCP dark pool fetch failed: %s' % type(e).__name__,
                  poll_ts=now)
        return {'ran': True, 'status': 'error', 'error': type(e).__name__}


def confirmation_for(symbol, direction, db, c, now):
    """Check stored dark pool prints for same-direction institutional activity.

    Returns {'confirmed': bool, 'notional': float, 'print_count': int, 'asof': ts}.
    Evidence only: a confirming input to Flow Pulse, never a trigger alone.
    """
    symbol = str(symbol or "").upper()
    latest = db.get(c, 'darkpool:latest', {}) or {}
    if latest.get('status') != 'ok':
        return {'confirmed': False, 'reason': latest.get('status') or 'no_data',
                'asof': latest.get('at')}
    prints = latest.get('prints') or []
    matched = [p for p in prints
               if p.get('symbol') == symbol
               and (p.get('ts') or 0) >= now - CONFIRM_WINDOW
               and (p.get('notional') or 0) >= MIN_NOTIONAL
               and (p.get('direction') in (None, direction))]
    # Direction-agnostic prints count as weak confirmation; same-direction is strong.
    strong = [p for p in matched if p.get('direction') == direction]
    notional = sum(p.get('notional') or 0 for p in matched)
    return {'confirmed': bool(matched),
            'strong': bool(strong),
            'notional': round(notional, 2),
            'print_count': len(matched),
            'asof': latest.get('at')}


def display(db, c, now):
    """Recent dark pool state for the dashboard panel."""
    latest = db.get(c, 'darkpool:latest', {})
    return {'asof': now, 'version': VERSION,
            'status': latest.get('status', 'waiting'),
            'tool': latest.get('tool'),
            'print_count': latest.get('print_count', 0),
            'symbols': latest.get('symbols', []),
            'prints': (latest.get('prints') or [])[:50],
            'last_scan': latest.get('at'),
            'note': 'Evidence only. Confirmation input for Flow Pulse.'}
