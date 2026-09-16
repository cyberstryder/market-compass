"""Read-only Alpaca quotes and durable edits of already-posted stock alerts."""
import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import and_, or_, select

from .expiration import VERSION as COMPARISON_VERSION, COST_MODEL, quality
from .store import canonical, insert_once, now_ms, option_jobs, outbox, signals

NY = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")
POLICY = {"version": "nearest_expiry_atm_call_v1", "min_dte": 1, "max_dte": 14,
          "strike_band_pct": 20, "tie_break": "lower_strike", "feed": "opra"}
PENDING_TEXT = "Option quote: retrieving from Alpaca…"


@dataclass
class OptionsConfig:
    enabled: bool = False
    key_id: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    paper: bool = True

    @classmethod
    def from_env(cls):
        return cls(os.getenv("OPTIONS_ENABLED", "false").lower() == "true",
                   os.getenv("APCA_API_KEY_ID", ""), os.getenv("APCA_API_SECRET_KEY", ""),
                   os.getenv("ALPACA_PAPER", "true").lower() == "true")

    def validate(self):
        if self.enabled and not (self.key_id.strip() and self.secret_key.strip()):
            raise RuntimeError("OPTIONS_ENABLED requires APCA_API_KEY_ID and APCA_API_SECRET_KEY")


class QuoteUnavailable(Exception):
    pass


def positive(value):
    if isinstance(value, bool):
        raise ValueError("not a number")
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0 or not math.isfinite(float(result)):
        raise ValueError("not positive")
    return result


def parse_contract(raw, underlying, day, stock_price, policy):
    """Validate provider metadata and OCC symbol together; exclude adjusted roots."""
    try:
        expiry = date.fromisoformat(raw["expiration_date"])
        strike = positive(raw["strike_price"])
        multiplier = positive(raw["size"])
        if (raw.get("underlying_symbol") != underlying or raw.get("root_symbol") != underlying
                or raw.get("type") != "call" or raw.get("status") != "active"
                or raw.get("tradable") is not True or multiplier != 100):
            return None
        if not policy["min_dte"] <= (expiry - day).days <= policy["max_dte"]:
            return None
        band = Decimal(str(policy["strike_band_pct"])) / 100
        if not stock_price * (1 - band) <= strike <= stock_price * (1 + band):
            return None
        encoded_strike = strike * 1000
        if encoded_strike != encoded_strike.to_integral_value() or encoded_strike >= 100000000:
            return None
        expected = underlying + expiry.strftime("%y%m%d") + "C" + f"{int(encoded_strike):08d}"
        if raw["symbol"] != expected:
            return None
        return {"symbol": expected, "underlying": underlying, "expiration": expiry.isoformat(),
                "strike": float(strike), "type": "call", "multiplier": 100,
                "dte_calendar": (expiry - day).days}
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return None


class AlpacaQuotes:
    def __init__(self, config, client, clock=now_ms, pace_seconds=0.35):
        self.config, self.client, self.clock = config, client, clock
        self.pace_seconds, self.last_request = pace_seconds, 0.0

    def get(self, url, params):
        pause = self.pace_seconds - (time.monotonic() - self.last_request)
        if pause > 0:
            time.sleep(pause)
        self.last_request = time.monotonic()
        # Never send Alpaca credentials to Discord or follow provider redirects.
        response = self.client.get(url, params=params, headers={
            "APCA-API-KEY-ID": self.config.key_id, "APCA-API-SECRET-KEY": self.config.secret_key,
        }, timeout=2, follow_redirects=False)
        if response.status_code != 200:
            reasons = {401: "alpaca_authentication_failed", 403: "alpaca_access_or_opra_entitlement_required",
                       429: "alpaca_rate_limited"}
            raise QuoteUnavailable(reasons.get(response.status_code, "alpaca_request_failed"))
        data = response.json()
        if not isinstance(data, dict):
            raise QuoteUnavailable("invalid_provider_response")
        return data

    def lookup(self, signal, policy=None, *, compare=False):
        policy = dict(policy or POLICY)
        result = {"status": "unavailable", "provider": "alpaca", "feed": "opra", "policy": policy,
                  "requested_at_ms": self.clock(), "signal_at_ms": signal["signal_at_ms"]}
        variants = {}
        try:
            if not self.config.enabled:
                raise QuoteUnavailable("options_disabled")
            if not self.config.key_id or not self.config.secret_key:
                raise QuoteUnavailable("alpaca_credentials_missing")
            if signal.get("is_test"):
                raise QuoteUnavailable("test_signal")
            # A current quote must not masquerade as the price of an old signal.
            if not -5000 <= self.clock() - signal["signal_at_ms"] <= 120000:
                raise QuoteUnavailable("signal_too_old_for_current_quote")
            underlying = signal["ticker"].split(":")[-1].upper()
            if not re.fullmatch(r"[A-Z]{1,6}", underlying):
                raise QuoteUnavailable("unsupported_underlying_symbol")
            if policy != POLICY:
                raise QuoteUnavailable("unsupported_selection_policy")
            day = datetime.fromtimestamp(signal["signal_at_ms"] / 1000, timezone.utc).astimezone(NY).date()
            price = positive(signal["price"])
            result["reference_stock_price"] = float(price)
            search_policy = dict(policy, min_dte=0) if compare else policy
            band = Decimal(str(policy["strike_band_pct"])) / 100
            params = {"underlying_symbols": underlying, "root_symbol": underlying, "type": "call",
                      "status": "active", "expiration_date_gte": (day + timedelta(days=search_policy["min_dte"])).isoformat(),
                      "expiration_date_lte": (day + timedelta(days=policy["max_dte"])).isoformat(),
                      "strike_price_gte": str(price * (1 - band)), "strike_price_lte": str(price * (1 + band)),
                      "limit": 1000}
            host = "https://paper-api.alpaca.markets" if self.config.paper else "https://api.alpaca.markets"
            contracts, seen = [], set()
            for page in range(3):
                data = self.get(host + "/v2/options/contracts", params)
                raw_contracts = data.get("option_contracts")
                if not isinstance(raw_contracts, list):
                    raise QuoteUnavailable("invalid_contract_response")
                for raw in raw_contracts:
                    contract = parse_contract(raw, underlying, day, price, search_policy)
                    if contract:
                        contracts.append(contract)
                token = data.get("next_page_token", data.get("page_token"))
                if not token:
                    break
                if not isinstance(token, str) or token in seen or page == 2:
                    raise QuoteUnavailable("contract_search_incomplete")
                seen.add(token)
                params["page_token"] = token
            specs = {"baseline": (1, 14, "no_eligible_contract_within_14_days")}
            if compare:
                specs.update(zero_dte=(0, 0, "no_eligible_same_day_contract"),
                             near_dte=(1, 3, "no_eligible_contract_within_1_to_3_days"))
            selected = {}
            for key, (lo, hi, reason) in specs.items():
                eligible = [c for c in contracts if lo <= c["dte_calendar"] <= hi]
                v = dict(result, policy=dict(policy, min_dte=lo, max_dte=hi,
                    version=policy["version"] if key == "baseline" else COMPARISON_VERSION + "_" + key), cost_model=dict(COST_MODEL))
                if eligible:
                    c = min(eligible, key=lambda c: (c["expiration"], abs(Decimal(str(c["strike"])) - price), c["strike"]))
                    v["contract"] = c
                    selected[c["symbol"]] = c
                else:
                    v["reason"] = reason
                variants[key] = v
            # One snapshot request for every distinct contract, including the baseline.
            observed = self.snapshots(list(selected.values())) if selected else {}
            for v in variants.values():
                if v.get("contract"):
                    v.update(observed[v["contract"]["symbol"]])
                    if compare and v.get("status") in {"available", "wide_spread"}:
                        if v["quote_at_ms"] < signal["signal_at_ms"]:
                            v.update(status="unavailable", reason="entry_quote_before_signal")
                        elif self.clock() - signal["signal_at_ms"] > 120000:
                            v.update(status="unavailable", reason="signal_too_old_for_current_quote")
            result = dict(variants["baseline"])
        except QuoteUnavailable as exc:
            result["status"] = "unavailable"
            result["reason"] = str(exc)  # Only our fixed, non-sensitive reason codes.
        except httpx.HTTPError:
            result["reason"] = "alpaca_network_error"
        except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation, OverflowError, OSError):
            result["status"] = "unavailable"
            result["reason"] = "invalid_provider_response"
        if compare:
            if not variants:
                variants = {k: dict(result, cost_model=dict(COST_MODEL)) for k in ("baseline", "zero_dte", "near_dte")}
            baseline_symbol = (variants["baseline"].get("contract") or {}).get("symbol")
            if baseline_symbol and baseline_symbol == (variants["near_dte"].get("contract") or {}).get("symbol"):
                variants["near_dte"]["alias_of"] = "baseline"
            result["expiration_comparison"] = {"version": COMPARISON_VERSION, "variants": variants}
        return result

    def read_snapshot(self, result, snapshot):
        quote = snapshot.get("latestQuote")
        if not isinstance(quote, dict):
            raise QuoteUnavailable("quote_missing")
        stamp = datetime.fromisoformat(quote["t"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise QuoteUnavailable("quote_timestamp_invalid")
        quote_ms = int(stamp.timestamp() * 1000)
        result.update(quote_at_ms=quote_ms, quote_age_seconds=(self.clock() - quote_ms) / 1000,
                      fetched_at_ms=self.clock())
        if not -5000 <= self.clock() - quote_ms <= 30000:
            raise QuoteUnavailable("quote_stale_or_future")
        bid, ask = positive(quote["bp"]), positive(quote["ap"])
        bid_size, ask_size = positive(quote["bs"]), positive(quote["as"])
        if bid > ask:
            raise QuoteUnavailable("quote_crossed")
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100
        result.update(status="wide_spread" if spread_pct > 20 else "available", bid=float(bid), ask=float(ask),
                      bid_size=float(bid_size), ask_size=float(ask_size), midpoint=float(mid),
                      spread_pct=float(spread_pct), ask_contract_cost=float(ask * 100))
        # Optional context is retained only when finite; it never validates a bad quote.
        greeks = snapshot.get("greeks")
        if isinstance(greeks, dict):
            result["greeks"] = {k: float(v) for k, v in greeks.items()
                if k in {"delta", "gamma", "theta", "vega", "rho"}
                and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}
        iv = snapshot.get("impliedVolatility")
        if isinstance(iv, (int, float)) and not isinstance(iv, bool) and math.isfinite(iv) and iv >= 0:
            result["implied_volatility"] = float(iv)

    def snapshots(self, contracts):
        """One OPRA request for at most 100 fixed contracts. No strike reselection."""
        if not 1 <= len(contracts) <= 100:
            raise ValueError("Batch requires 1-100 contracts")
        results = {c["symbol"]: {"status": "unavailable", "provider": "alpaca", "feed": "opra",
                   "contract": c, "requested_at_ms": self.clock()} for c in contracts}
        try:
            if not self.config.enabled:
                raise QuoteUnavailable("options_disabled")
            data = self.get("https://data.alpaca.markets/v1beta1/options/snapshots",
                            {"symbols": ",".join(results), "feed": "opra", "limit": 100})
            snapshots = data.get("snapshots")
            if not isinstance(snapshots, dict) or data.get("next_page_token"):
                raise QuoteUnavailable("invalid_or_incomplete_snapshot_batch")
        except QuoteUnavailable as exc:
            for q in results.values():
                q["reason"] = str(exc)
            return results
        except httpx.HTTPError:
            for q in results.values():
                q["reason"] = "alpaca_network_error"
            return results
        except (ValueError, TypeError):
            for q in results.values():
                q["reason"] = "invalid_provider_response"
            return results
        for symbol, result in results.items():
            try:
                self.read_snapshot(result, snapshots.get(symbol, {}))
            except QuoteUnavailable as exc:
                result["status"] = "unavailable"
                result["reason"] = str(exc)
            except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation, OverflowError, OSError):
                result["status"] = "unavailable"
                result["reason"] = "invalid_provider_response"
        return results


def option_text(q):
    lines = ["**OPTION QUOTE UPDATE | research comparison**"]
    if q.get("status") not in {"available", "wide_spread"}:
        lines.append("Option quote unavailable: " + q.get("reason", "unknown").replace("_", " ") + ".")
    else:
        c, cost = q["contract"], quality(q)
        stamp = datetime.fromtimestamp(q["quote_at_ms"] / 1000, timezone.utc).astimezone(CT)
        lines.extend([f"Current: {c['underlying']} ${c['strike']:g} CALL · {c['expiration']}",
            f"Bid ${q['bid']:.2f} | Ask ${q['ask']:.2f} | Mid ${q['midpoint']:.2f}",
            f"Premium ${cost['premium_at_ask']:.2f}/contract | Spread ${cost['spread_per_contract']:.2f} ({cost['spread_pct']:.1f}%)",
            f"Spread + assumed round-trip fees: ${cost['spread_and_fees']:.2f}/contract",
            f"OPRA {stamp:%H:%M:%S} CT; closest strike, nearest expiry after today."])
        if cost["approx_stock_cost_hurdle"] is not None:
            lines.append(f"Approx stock move to cover costs: ${cost['approx_stock_cost_hurdle']:.3f} (fixed delta; ignores IV/theta/gamma).")
        if q["status"] == "wide_spread":
            lines.append("Wide spread — high entry/exit cost.")
    comparison = q.get("expiration_comparison")
    if comparison:
        for key, label in (("zero_dte", "0DTE"), ("near_dte", "1–3 DTE")):
            v = comparison["variants"][key]
            if v.get("alias_of"):
                lines.append(f"{label}: same contract as current; counted once.")
            elif v.get("status") not in {"available", "wide_spread"}:
                lines.append(f"{label}: unavailable ({v.get('reason', 'quote unavailable').replace('_', ' ')}).")
            else:
                c, cost = v["contract"], quality(v)
                lines.append(f"{label}: ${c['strike']:g} CALL {c['expiration']} | Ask ${v['ask']:.2f} (${cost['premium_at_ask']:.2f}/contract) | Spread ${cost['spread_per_contract']:.2f} ({cost['spread_pct']:.1f}%)" + (" WIDE" if v["status"] == "wide_spread" else ""))
    lines.append("CALL bias is stock direction, not an option recommendation. Quote only; no fill assumed. Fees assume $0.65/side; DTE = calendar days.")
    return "\n".join(lines)


def enqueue_option(conn, event_id, message_id, policy):
    sid = event_id.removesuffix("-signal")
    # No guesswork if Discord didn't return a valid message identity.
    valid = isinstance(message_id, str) and re.fullmatch(r"[0-9]{1,32}", message_id)
    insert_once(conn, option_jobs, {"event_id": event_id, "signal_id": sid,
        "message_id": message_id if valid else None, "policy_json": canonical(policy),
        "status": "pending" if valid else "complete",
        "quote_json": None if valid else canonical({"status": "unavailable", "reason": "discord_message_id_missing"}),
        "attempts": 0, "lease_until_ms": 0})


def enrich_one(engine, provider, now=None):
    now = now or now_ms()
    with engine.begin() as conn:
        row = conn.execute(select(option_jobs).where(or_(option_jobs.c.status == "pending",
            and_(option_jobs.c.status == "fetching", option_jobs.c.lease_until_ms <= now)))
            .order_by(option_jobs.c.event_id).limit(1).with_for_update(skip_locked=True)).mappings().first()
        if row is None:
            return False
        job, lease = dict(row), str(uuid.uuid4())
        conn.execute(option_jobs.update().where(option_jobs.c.event_id == job["event_id"]).values(
            status="fetching", lease_token=lease, lease_until_ms=now + 30000, attempts=job["attempts"] + 1))
        saved = conn.execute(select(signals).where(signals.c.signal_id == job["signal_id"])).mappings().one()
        signal = json.loads(saved["signal_json"])
        original = json.loads(conn.execute(select(outbox.c.payload_json).where(outbox.c.event_id == job["event_id"])).scalar_one())
    if job["attempts"] >= 3:
        quote = {"status": "unavailable", "reason": "option_worker_retry_limit"}
    else:
        quote = provider.lookup(signal, json.loads(job["policy_json"]), compare=True)
    original.pop("_option_policy", None)
    original["content"] = original["content"].replace("\n" + PENDING_TEXT, "") + "\n\n" + option_text(quote)
    original["_edit_message_id"] = job["message_id"]
    with engine.begin() as conn:
        updated = conn.execute(option_jobs.update().where(and_(option_jobs.c.event_id == job["event_id"],
            option_jobs.c.lease_token == lease)).values(status="complete", quote_json=canonical(quote),
            lease_until_ms=0, lease_token=None).returning(option_jobs.c.event_id)).first()
        if updated:
            from .option_history import schedule_history
            schedule_history(conn, signal, quote, getattr(provider, "clock", lambda: now)())
            insert_once(conn, outbox, {"event_id": job["event_id"] + "-option", "payload_json": canonical(original),
                "status": "pending", "attempts": 0, "next_attempt_ms": now_ms(), "lease_until_ms": 0})
    return True


def run_options_worker(engine, config, stop):
    with httpx.Client(timeout=2, follow_redirects=False) as client:
        provider = AlpacaQuotes(config, client)
        while not stop.is_set():
            try:
                worked = enrich_one(engine, provider)
            except Exception:
                worked = False  # Durable lease recovers; never log credentials/URLs.
            stop.wait(0.2 if worked else 1)
