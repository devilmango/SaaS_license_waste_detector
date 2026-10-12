"""Project contract pool charges across scheduled mid-term changes."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

from .engine import CurrencyConfig, InputError, _rows


EVENT_TYPES = {"seat_add", "seat_remove", "entitlement_change", "price_change"}
DAY_BASIS = Decimal("365")


@dataclass(frozen=True)
class ContractModelRow:
    contract_id: str
    seat_pools: str
    applications: str
    period_start: date
    period_end: date
    opening_active_seats: int
    ending_active_seats: int
    opening_entitled_seats: int
    ending_entitled_seats: int
    ending_overage_seats: int
    annualized_cost_at_start_inr: Decimal
    annualized_cost_at_end_inr: Decimal
    prorated_period_cost_inr: Decimal
    applied_change_count: int
    status: str


@dataclass(frozen=True)
class _Change:
    contract_key: str
    pool_key: str
    effective_date: date
    event_type: str
    amount: int
    price_inr: Decimal | None


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


def _integer(value: str, path: Path, line: int, field: str, *, signed: bool = False) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise InputError(f"{path}:{line}: {field} must be a whole number") from exc
    if (not signed and result <= 0) or (signed and result == 0):
        expectation = "non-zero" if signed else "greater than zero"
        raise InputError(f"{path}:{line}: {field} must be {expectation}")
    return result


def _decimal(value: str, path: Path, line: int, field: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise InputError(f"{path}:{line}: {field} must be numeric") from exc
    if not result.is_finite() or result < 0:
        raise InputError(f"{path}:{line}: {field} must be finite and non-negative")
    return result


def _pool_key(contract_id: str, pool_id: str, application: str) -> str:
    return f"{_norm(contract_id)}::{_norm(pool_id) if pool_id else _norm(application)}"


def _entitlement_for(pool: dict[str, object], seats: int, path: Path) -> int:
    tier_rows = pool["entitlement_tiers"]
    assert isinstance(tier_rows, list)
    matching = next((item for item in tier_rows if seats >= item[0] and (item[1] is None or seats <= item[1])), None)
    if matching is None and tier_rows and seats < tier_rows[0][0]:
        matching = tier_rows[0]
    if matching is None:
        raise InputError(f"{path}: no entitlement tier covers {seats} seats in pool {pool['pool_id']!r}")
    return matching[2]


def _round(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _read_contract_pools(
    entitlements_path: Path, billing_path: Path,
) -> tuple[dict[str, dict[str, object]], dict[tuple[str, str], str], dict[str, int]]:
    entitlement_rows = _rows(entitlements_path, {"contract_id", "application", "entitled_seats"})
    pools: dict[str, dict[str, object]] = {}
    app_to_pool: dict[tuple[str, str], str] = {}
    for line, row in enumerate(entitlement_rows, start=2):
        contract_id = row.get("contract_id", "").strip()
        application = row.get("application", "").strip()
        if not contract_id or not application:
            raise InputError(f"{entitlements_path}:{line}: contract_id and application cannot be blank")
        try:
            entitled = int(row.get("entitled_seats", ""))
        except ValueError as exc:
            raise InputError(f"{entitlements_path}:{line}: entitled_seats must be a whole number") from exc
        if entitled < 0:
            raise InputError(f"{entitlements_path}:{line}: entitled_seats cannot be negative")
        tiered = bool(row.get("min_seats", "").strip() or row.get("max_seats", "").strip())
        entitlement_tier = None
        if tiered:
            try:
                tier_min = int(row.get("min_seats", "") or "0")
                tier_max = int(row["max_seats"]) if row.get("max_seats", "").strip() else None
            except ValueError as exc:
                raise InputError(f"{entitlements_path}:{line}: entitlement tier bounds must be whole numbers") from exc
            if tier_min < 0 or (tier_max is not None and tier_max < tier_min):
                raise InputError(f"{entitlements_path}:{line}: invalid entitlement tier range")
            entitlement_tier = (tier_min, tier_max, entitled)
        pool_id = row.get("pool_id", "").strip()
        key = _pool_key(contract_id, pool_id, application)
        state = pools.setdefault(key, {
            "contract_id": contract_id, "contract_key": _norm(contract_id),
            "pool_id": pool_id or application, "applications": set(), "entitled": entitled,
            "entitlement_tiers": [], "tiers_by_application": {},
            "entitlement_mode": "tiered" if tiered else "fixed",
            "identities": set(),
        })
        if state["entitlement_mode"] != ("tiered" if tiered else "fixed"):
            raise InputError(f"{entitlements_path}:{line}: cannot mix fixed and tiered entitlements in a pool")
        if tiered:
            tier_rows = state["entitlement_tiers"]
            assert isinstance(tier_rows, list)
            if entitlement_tier in tier_rows:
                pass  # The same pool tier is repeated for another included application.
            elif any(item[0] == tier_min for item in tier_rows):
                raise InputError(f"{entitlements_path}:{line}: conflicting entitlement tier for shared pool {pool_id!r}")
            else:
                tier_rows.append(entitlement_tier)
            by_app = state["tiers_by_application"]
            assert isinstance(by_app, dict)
            app_tiers = by_app.setdefault(_norm(application), set())
            app_tiers.add(entitlement_tier)
        elif int(state["entitled"]) != entitled:
            raise InputError(f"{entitlements_path}:{line}: entitlement differs within shared pool {pool_id!r}")
        apps = state["applications"]
        assert isinstance(apps, set)
        app_key = (_norm(contract_id), _norm(application))
        if app_key in app_to_pool and app_to_pool[app_key] != key:
            raise InputError(f"{entitlements_path}:{line}: application maps to multiple seat pools")
        apps.add(application)
        app_to_pool[app_key] = key

    billing_rows = _rows(billing_path, {"contract_id", "application", "license_status"})
    if not {"email", "employee_id"}.intersection(billing_rows[0] if billing_rows else {}):
        raise InputError(f"{billing_path}: billing rows must include email or employee_id for seat counting")
    minimums: dict[str, int] = {}
    for line, row in enumerate(billing_rows, start=2):
        contract_id = row.get("contract_id", "").strip()
        application = row.get("application", "").strip()
        if not contract_id or not application:
            raise InputError(f"{billing_path}:{line}: active license requires contract_id and application")
        contract_key = _norm(contract_id)
        minimum_text = row.get("seat_minimum", "").strip()
        try:
            minimum = int(minimum_text or "0")
        except ValueError as exc:
            raise InputError(f"{billing_path}:{line}: seat_minimum must be a whole number") from exc
        if minimum < 0:
            raise InputError(f"{billing_path}:{line}: seat_minimum cannot be negative")
        if contract_key in minimums and minimums[contract_key] != minimum:
            raise InputError(f"{billing_path}:{line}: seat_minimum conflicts within contract {contract_id!r}")
        minimums[contract_key] = minimum
        if row.get("license_status", "").casefold() != "active":
            continue
        if not row.get("email", "").strip() and not row.get("employee_id", "").strip():
            raise InputError(f"{billing_path}:{line}: active license requires an employee identity")
        pool_id = row.get("seat_pool_id", "").strip()
        mapped_pool = app_to_pool.get((contract_key, _norm(application)))
        pool_key = _pool_key(contract_id, pool_id, application) if pool_id else mapped_pool or _pool_key(
            contract_id, "", application,
        )
        if pool_id and mapped_pool and mapped_pool != pool_key:
            raise InputError(f"{billing_path}:{line}: seat_pool_id conflicts with the entitlement pool mapping")
        pool = pools.setdefault(pool_key, {
            "contract_id": contract_id, "contract_key": contract_key,
            "pool_id": pool_id or application, "applications": set(), "entitled": 0,
            "entitlement_tiers": [], "tiers_by_application": {},
            "entitlement_mode": "fixed", "identities": set(),
        })
        if pool_id and _norm(str(pool["pool_id"])) != _norm(pool_id):
            raise InputError(f"{billing_path}:{line}: conflicting seat_pool_id values")
        apps = pool["applications"]
        identities = pool["identities"]
        assert isinstance(apps, set) and isinstance(identities, set)
        apps.add(application)
        identity = row.get("employee_id", "").strip() or row.get("email", "").strip()
        identities.add(_norm(identity))
    for pool in pools.values():
        tier_rows = pool["entitlement_tiers"]
        assert isinstance(tier_rows, list)
        ordered = sorted(tier_rows, key=lambda item: item[0])
        for left, right in zip(ordered, ordered[1:]):
            if left[1] is None or right[0] <= left[1]:
                raise InputError(f"{entitlements_path}: overlapping entitlement tiers for pool {pool['pool_id']!r}")
        pool["entitlement_tiers"] = ordered
        if pool["entitlement_mode"] == "tiered":
            by_app = pool["tiers_by_application"]
            assert isinstance(by_app, dict)
            schedules = {frozenset(values) for values in by_app.values()}
            if len(schedules) > 1:
                raise InputError(f"{entitlements_path}: applications in shared pool {pool['pool_id']!r} have different entitlement tiers")
            active_seats = len(pool["identities"])
            pool["entitled"] = _entitlement_for(pool, active_seats, entitlements_path)
    return pools, app_to_pool, minimums


def _read_tiers(
    pricing_path: Path, currency: CurrencyConfig,
) -> dict[str, tuple[tuple[int, int | None, Decimal], ...]]:
    rows = _rows(pricing_path, {"contract_id", "min_seats", "annual_cost_per_seat"})
    grouped: dict[str, list[tuple[int, int | None, Decimal]]] = {}
    for line, row in enumerate(rows, start=2):
        contract_id = row.get("contract_id", "").strip()
        if not contract_id:
            raise InputError(f"{pricing_path}:{line}: contract_id cannot be blank")
        try:
            minimum = int(row.get("min_seats", ""))
            maximum = int(row["max_seats"]) if row.get("max_seats", "").strip() else None
        except ValueError as exc:
            raise InputError(f"{pricing_path}:{line}: tier seat bounds must be whole numbers") from exc
        if minimum < 0 or (maximum is not None and maximum < minimum):
            raise InputError(f"{pricing_path}:{line}: invalid tier seat range")
        rate = _decimal(row.get("annual_cost_per_seat", ""), pricing_path, line, "annual_cost_per_seat")
        rate = currency.convert(rate, row.get("currency", ""), pricing_path, line)
        grouped.setdefault(_norm(contract_id), []).append((minimum, maximum, rate))
    result = {}
    for contract, values in grouped.items():
        ordered = sorted(values, key=lambda item: item[0])
        for left, right in zip(ordered, ordered[1:]):
            if left[1] is None or right[0] <= left[1]:
                raise InputError(f"{pricing_path}: overlapping tiers for contract {contract!r}")
        result[contract] = tuple(ordered)
    return result


def _read_changes(
    changes_path: Path, currency: CurrencyConfig, period_start: date, period_end: date,
    pools: dict[str, dict[str, object]],
) -> dict[str, list[_Change]]:
    rows = _rows(changes_path, {"contract_id", "effective_date", "event_type"})
    by_contract: dict[str, list[str]] = {}
    for key, pool in pools.items():
        by_contract.setdefault(str(pool["contract_key"]), []).append(key)
    changes: dict[str, list[_Change]] = {}
    for line, row in enumerate(rows, start=2):
        contract_id = row.get("contract_id", "").strip()
        contract_key = _norm(contract_id)
        event_type = row.get("event_type", "").strip().casefold()
        if not contract_id or event_type not in EVENT_TYPES:
            raise InputError(f"{changes_path}:{line}: contract_id or event_type is invalid")
        try:
            effective = date.fromisoformat(row.get("effective_date", ""))
        except ValueError as exc:
            raise InputError(f"{changes_path}:{line}: effective_date must use YYYY-MM-DD") from exc
        pool_id = row.get("pool_id", "").strip()
        candidates = by_contract.get(contract_key, [])
        if not candidates:
            raise InputError(f"{changes_path}:{line}: unknown contract {contract_id!r}")
        if pool_id:
            matches = [key for key in candidates if _norm(str(pools[key]["pool_id"])) == _norm(pool_id)]
            if len(matches) != 1:
                raise InputError(f"{changes_path}:{line}: pool_id does not identify one contract pool")
            pool_key = matches[0]
        elif event_type == "price_change":
            pool_key = ""
        elif len(candidates) == 1:
            pool_key = candidates[0]
        else:
            raise InputError(f"{changes_path}:{line}: pool_id is required for changes to a multi-pool contract")
        amount = 0
        price = None
        if event_type in {"seat_add", "seat_remove"}:
            amount = _integer(row.get("seat_delta", ""), changes_path, line, "seat_delta")
        elif event_type == "entitlement_change":
            amount = _integer(row.get("entitlement_delta", ""), changes_path, line, "entitlement_delta", signed=True)
        elif event_type == "price_change":
            price = _decimal(row.get("annual_cost_per_seat", ""), changes_path, line, "annual_cost_per_seat")
            price = currency.convert(price, row.get("currency", ""), changes_path, line)
        if period_start <= effective < period_end:
            changes.setdefault(contract_key, []).append(_Change(
                contract_key, pool_key, effective, event_type, amount, price,
            ))
    for values in changes.values():
        event_order = {"seat_add": 0, "seat_remove": 0, "entitlement_change": 1, "price_change": 2}
        values.sort(key=lambda change: (change.effective_date, event_order[change.event_type], change.event_type, change.pool_key))
    return changes


def _tier_rate(
    tiers: tuple[tuple[int, int | None, Decimal], ...], seats: int, contract_id: str,
) -> Decimal:
    for minimum, maximum, rate in tiers:
        if seats >= minimum and (maximum is None or seats <= maximum):
            return rate
    raise InputError(f"No configured contract price tier covers {seats} seats for {contract_id!r}")


def model_contract_period(
    billing_path: Path,
    entitlements_path: Path,
    pricing_path: Path,
    changes_path: Path,
    period_start: date,
    period_end: date,
    *,
    currency: CurrencyConfig | None = None,
) -> tuple[ContractModelRow, ...]:
    """Estimate contract charges across an invoice period, applying effective-dated changes."""
    if period_end <= period_start:
        raise InputError("period_end must be after period_start")
    currency = currency or CurrencyConfig(rates_to_inr={"INR": Decimal("1")})
    pools, _, minimums = _read_contract_pools(entitlements_path, billing_path)
    tiers = _read_tiers(pricing_path, currency)
    changes = _read_changes(changes_path, currency, period_start, period_end, pools)
    contracts: dict[str, list[str]] = {}
    for pool_key, pool in pools.items():
        contracts.setdefault(str(pool["contract_key"]), []).append(pool_key)
    rows: list[ContractModelRow] = []
    period_days = (period_end - period_start).days
    for contract_key, pool_keys in sorted(contracts.items()):
        first_pool = pools[pool_keys[0]]
        contract_id = str(first_pool["contract_id"])
        contract_tiers = tiers.get(contract_key)
        if not contract_tiers:
            raise InputError(f"{pricing_path}: no price tiers configured for contract {contract_id!r}")
        minimum = minimums.get(contract_key, 0)
        active = {key: len(pools[key]["identities"]) for key in pool_keys}
        entitled = {key: int(pools[key]["entitled"]) for key in pool_keys}
        entitlement_adjustments = {key: 0 for key in pool_keys}
        opening_active = sum(active.values())
        opening_entitled = sum(entitled.values())
        events = changes.get(contract_key, [])
        event_idx = 0
        cursor = period_start
        price_override: Decimal | None = None
        total_cost = Decimal("0")
        initial_rate = Decimal("0")
        final_rate = Decimal("0")
        applied = 0
        while cursor < period_end:
            todays = []
            while event_idx < len(events) and events[event_idx].effective_date == cursor:
                todays.append(events[event_idx])
                event_idx += 1
            for change in todays:
                if change.event_type == "price_change":
                    price_override = change.price_inr
                elif change.event_type in {"seat_add", "seat_remove"}:
                    pool_key = change.pool_key
                    updated = active[pool_key] + change.amount * (1 if change.event_type == "seat_add" else -1)
                    if updated < 0:
                        raise InputError(f"{changes_path}: seat removal exceeds active seats for pool {pools[pool_key]['pool_id']!r}")
                    active[pool_key] = updated
                    tier_rows = pools[pool_key]["entitlement_tiers"]
                    if tier_rows:
                        entitled[pool_key] = _entitlement_for(
                            pools[pool_key], updated, entitlements_path,
                        ) + entitlement_adjustments[pool_key]
                else:
                    pool_key = change.pool_key
                    updated = entitled[pool_key] + change.amount
                    if updated < 0:
                        raise InputError(f"{changes_path}: entitlement change makes pool {pools[pool_key]['pool_id']!r} negative")
                    entitled[pool_key] = updated
                    entitlement_adjustments[pool_key] += change.amount
                applied += 1
            next_date = events[event_idx].effective_date if event_idx < len(events) else period_end
            next_date = min(next_date, period_end)
            seats = sum(active.values())
            billable_seats = max(seats, minimum)
            rate = Decimal("0") if billable_seats == 0 else price_override if price_override is not None else _tier_rate(
                contract_tiers, billable_seats, contract_id,
            )
            annualized = Decimal(billable_seats) * rate
            segment_days = (next_date - cursor).days
            total_cost += annualized * Decimal(segment_days) / DAY_BASIS
            if cursor == period_start:
                initial_rate = annualized
            final_rate = annualized
            cursor = next_date
            if segment_days == 0 and not todays and event_idx >= len(events):
                break
        ending_active = sum(active.values())
        ending_entitled = sum(entitled.values())
        overage = max(ending_active - ending_entitled, 0)
        status = "over_entitlement" if overage else "within_entitlement"
        if ending_active < minimum:
            status += "+below_contract_minimum"
        rows.append(ContractModelRow(
            contract_id,
            ";".join(sorted({str(pools[key]["pool_id"]) for key in pool_keys}, key=str.casefold)),
            ";".join(sorted({app for key in pool_keys for app in pools[key]["applications"]}, key=str.casefold)),
            period_start, period_end, opening_active, ending_active, opening_entitled,
            ending_entitled, overage, _round(initial_rate), _round(final_rate), _round(total_cost),
            applied, status,
        ))
    return tuple(rows)


def format_contract_model(rows: Iterable[ContractModelRow]) -> str:
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "contract_id", "seat_pools", "applications", "period_start", "period_end",
        "opening_active_seats", "ending_active_seats", "opening_entitled_seats",
        "ending_entitled_seats", "ending_overage_seats", "annualized_cost_at_start_inr",
        "annualized_cost_at_end_inr", "prorated_period_cost_inr", "applied_change_count", "status",
    ])
    for row in rows:
        writer.writerow([
            row.contract_id, row.seat_pools, row.applications, row.period_start.isoformat(),
            row.period_end.isoformat(), row.opening_active_seats, row.ending_active_seats,
            row.opening_entitled_seats, row.ending_entitled_seats, row.ending_overage_seats,
            f"{row.annualized_cost_at_start_inr:.2f}", f"{row.annualized_cost_at_end_inr:.2f}",
            f"{row.prorated_period_cost_inr:.2f}", row.applied_change_count, row.status,
        ])
    return output.getvalue()
