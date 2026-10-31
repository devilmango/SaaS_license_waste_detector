"""Usage-based license tier recommendations and approval-gated downgrades."""

from __future__ import annotations

from contextlib import contextmanager
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
from pathlib import Path
from typing import Callable

from .engine import InputError, _rows


TIER_FIELDS = ("application", "tier", "feature_capacity", "annual_cost_inr", "provider_tier_id")
EVENT_FIELDS = ("action_id", "event", "status", "application", "email", "provider", "provider_user_id",
                "current_tier", "recommended_tier", "current_provider_tier_id",
                "recommended_provider_tier_id", "annual_savings_inr", "actor", "proposed_by",
                "approved_by", "occurred_at", "result")


@dataclass(frozen=True)
class TierRecommendation:
    action_id: str
    employee: str
    email: str
    application: str
    provider: str
    provider_user_id: str
    current_tier: str
    recommended_tier: str
    current_provider_tier_id: str
    recommended_provider_tier_id: str
    features_used: int
    annual_cost_current_inr: Decimal
    annual_cost_recommended_inr: Decimal
    annual_savings_inr: Decimal
    status: str = "proposed"


def recommend_downgrades(usage_path: Path, billing_path: Path, tiers_path: Path) -> tuple[TierRecommendation, ...]:
    """Choose the cheapest tier that supports observed feature usage and costs less."""
    usage = _rows(usage_path, {"employee", "email", "application", "features_used"})
    billing = _rows(billing_path, {"email", "application", "license_status", "annual_cost", "license_tier"})
    tier_rows = _rows(tiers_path, set(TIER_FIELDS))
    catalogs: dict[str, list[dict[str, str]]] = {}
    for line, row in enumerate(tier_rows, start=2):
        app = _norm(row["application"])
        try:
            capacity = int(row["feature_capacity"])
            price = Decimal(row["annual_cost_inr"])
        except (ValueError, InvalidOperation) as exc:
            raise InputError(f"{tiers_path}:{line}: feature_capacity and annual_cost_inr must be numeric") from exc
        if capacity < 0 or not price.is_finite() or price < 0 or not row["tier"].strip() or not row["provider_tier_id"].strip():
            raise InputError(f"{tiers_path}:{line}: tier, provider tier ID, non-negative capacity, and price are required")
        catalog = catalogs.setdefault(app, [])
        if any(_norm(previous["tier"]) == _norm(row["tier"]) for previous in catalog):
            raise InputError(f"{tiers_path}:{line}: duplicate application/tier price row")
        catalog.append(row | {"_capacity": str(capacity), "_price": str(price)})
    usage_by_key: dict[tuple[str, str], dict[str, str]] = {}
    seen_usage: set[tuple[str, str]] = set()
    for line, row in enumerate(usage, start=2):
        key = (_norm(row["email"]), _norm(row["application"]))
        if not key[0]:
            raise InputError(f"{usage_path}:{line}: email is required for tier optimization")
        if key in seen_usage:
            raise InputError(f"{usage_path}:{line}: duplicate usage identity/application")
        seen_usage.add(key)
        if not row["features_used"].strip():
            # Missing provider telemetry is not evidence for a downgrade.
            continue
        try:
            count = int(row["features_used"])
        except ValueError as exc:
            raise InputError(f"{usage_path}:{line}: features_used must be a whole number") from exc
        if count < 0:
            raise InputError(f"{usage_path}:{line}: features_used cannot be negative")
        usage_by_key[key] = row | {"_features": str(count)}
    recommendations: list[TierRecommendation] = []
    seen: set[tuple[str, str]] = set()
    for line, license_row in enumerate(billing, start=2):
        if license_row["license_status"].casefold() != "active":
            continue
        key = (_norm(license_row["email"]), _norm(license_row["application"]))
        if not key[0] or key not in usage_by_key:
            continue
        if key in seen:
            raise InputError(f"{billing_path}:{line}: duplicate active tier assignment")
        seen.add(key)
        catalog = catalogs.get(key[1], [])
        current = next((row for row in catalog if _norm(row["tier"]) == _norm(license_row["license_tier"])), None)
        if current is None:
            continue
        activity = usage_by_key[key]
        features = int(activity["_features"])
        supported = [row for row in catalog if features <= int(row["_capacity"]) < int(current["_capacity"])]
        if not supported:
            continue
        lower = min(supported, key=lambda row: (Decimal(row["_price"]), int(row["_capacity"])))
        try:
            current_cost = Decimal(license_row["annual_cost"])
        except InvalidOperation as exc:
            raise InputError(f"{billing_path}:{line}: annual_cost must be numeric") from exc
        if not current_cost.is_finite() or current_cost < 0:
            raise InputError(f"{billing_path}:{line}: annual_cost must be finite and non-negative")
        saving = (current_cost - Decimal(lower["_price"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if _norm(lower["tier"]) == _norm(current["tier"]) or saving <= 0:
            continue
        provider = license_row.get("provider", "").casefold()
        user_id = license_row.get("provider_user_id", "").strip()
        raw = "\0".join((key[0], key[1], current["tier"], lower["tier"],
                           current["provider_tier_id"], lower["provider_tier_id"], provider, user_id))
        action_id = hashlib.sha256(raw.encode()).hexdigest()[:20]
        recommendations.append(TierRecommendation(
            action_id, activity.get("employee", ""), license_row["email"], license_row["application"],
            provider, user_id, current["tier"], lower["tier"], current["provider_tier_id"],
            lower["provider_tier_id"], features, current_cost,
            Decimal(lower["_price"]), saving,
        ))
    return tuple(sorted(recommendations, key=lambda item: (-item.annual_savings_inr, item.application.casefold(), item.email.casefold())))


def format_recommendations(rows: tuple[TierRecommendation, ...]) -> str:
    from io import StringIO
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["action_id", "employee", "email", "application", "provider", "provider_user_id",
                     "current_tier", "recommended_tier", "current_provider_tier_id",
                     "recommended_provider_tier_id", "features_used", "annual_cost_current_inr",
                     "annual_cost_recommended_inr", "annual_savings_inr", "status"])
    for row in rows:
        writer.writerow([row.action_id, row.employee, row.email, row.application, row.provider,
                         row.provider_user_id, row.current_tier, row.recommended_tier,
                         row.current_provider_tier_id, row.recommended_provider_tier_id, row.features_used,
                         f"{row.annual_cost_current_inr:.2f}", f"{row.annual_cost_recommended_inr:.2f}",
                         f"{row.annual_savings_inr:.2f}", row.status])
    return output.getvalue()


def _events(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not set(EVENT_FIELDS).issubset(reader.fieldnames or []):
                raise InputError(f"{path}: tier action ledger is missing required columns")
            rows = list(reader)
    except OSError as exc:
        raise InputError(f"Cannot read tier action ledger {path}: {exc}") from exc
    states: dict[str, dict[str, str]] = {}
    for line, event in enumerate(rows, start=2):
        action_id, kind = event.get("action_id", ""), event.get("event", "")
        if not action_id or kind not in {"proposed", "approved", "rejected", "execution_started", "executed", "failed"}:
            raise InputError(f"{path}:{line}: invalid tier action event")
        prior = states.get(action_id)
        if kind == "proposed" and prior:
            raise InputError(f"{path}:{line}: duplicate tier recommendation")
        if kind in {"approved", "rejected"} and (not prior or prior["status"] != "proposed"):
            raise InputError(f"{path}:{line}: only proposed tier actions may be approved or rejected")
        if kind == "approved" and event.get("approved_by", "").casefold() == event.get("proposed_by", "").casefold():
            raise InputError(f"{path}:{line}: tier action approval must be by a different person than the proposer")
        if kind == "execution_started" and (not prior or prior["status"] != "approved"):
            raise InputError(f"{path}:{line}: only approved tier actions may execute")
        if kind in {"executed", "failed"} and (not prior or prior["status"] != "execution_started"):
            raise InputError(f"{path}:{line}: execution result has no start event")
        states[action_id] = event
    return states


def _append(path: Path, event: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EVENT_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({field: event.get(field, "") for field in EVENT_FIELDS})


def stage_recommendations(rows: tuple[TierRecommendation, ...], path: Path, *, actor: str) -> int:
    with _ledger_lock(path):
        return _stage_recommendations(rows, path, actor=actor)


def _stage_recommendations(rows: tuple[TierRecommendation, ...], path: Path, *, actor: str) -> int:
    if not actor.strip():
        raise InputError("tier recommendation actor is required")
    states = _events(path)
    created = 0
    for row in rows:
        if row.action_id in states:
            continue
        event = {"action_id": row.action_id, "event": "proposed", "status": "proposed",
                 "application": row.application, "email": row.email, "provider": row.provider,
                 "provider_user_id": row.provider_user_id, "current_tier": row.current_tier,
                 "recommended_tier": row.recommended_tier,
                 "current_provider_tier_id": row.current_provider_tier_id,
                 "recommended_provider_tier_id": row.recommended_provider_tier_id,
                 "annual_savings_inr": f"{row.annual_savings_inr:.2f}",
                 "actor": actor.strip(), "proposed_by": actor.strip(),
                 "occurred_at": datetime.now(timezone.utc).isoformat()}
        _append(path, event)
        states[row.action_id] = event
        created += 1
    return created


def list_tier_actions(path: Path) -> tuple[dict[str, str], ...]:
    return tuple(_events(path).values())


def transition_tier_action(path: Path, action_id: str, event: str, *, actor: str,
                           operation: Callable[[dict[str, str]], str] | None = None) -> dict[str, str]:
    with _ledger_lock(path):
        return _transition_tier_action(path, action_id, event, actor=actor, operation=operation)


def _transition_tier_action(path: Path, action_id: str, event: str, *, actor: str,
                            operation: Callable[[dict[str, str]], str] | None = None) -> dict[str, str]:
    if event not in {"approved", "rejected", "execute"} or not actor.strip():
        raise InputError("supported tier action and actor are required")
    state = _events(path).get(action_id)
    if not state:
        raise InputError(f"unknown tier action {action_id!r}; stage recommendations first")
    target_event = "execution_started" if event == "execute" else event
    if event == "execute" and state["status"] != "approved":
        raise InputError("provider execution requires a separately approved tier action")
    if event in {"approved", "rejected"} and state["status"] != "proposed":
        raise InputError("only proposed tier actions can be approved or rejected")
    if event == "approved" and actor.strip().casefold() == state.get("proposed_by", state.get("actor", "")).casefold():
        raise InputError("tier action approval must be by a different person than the proposer")
    if event == "execute":
        if (state["provider"] != "zoom" or state["current_provider_tier_id"] != "2"
                or state["recommended_provider_tier_id"] != "1"):
            raise InputError("provider execution currently supports only Zoom Licensed (2) to Basic (1) downgrades")
        if not state["provider_user_id"] or operation is None:
            raise InputError("Zoom provider operation and imported user ID are required")
        started = state | {"event": "execution_started", "status": "execution_started", "actor": actor.strip(),
                           "occurred_at": datetime.now(timezone.utc).isoformat(), "result": ""}
        _append(path, started)
        try:
            result = operation(state)
        except Exception as exc:
            failed = started | {"event": "failed", "status": "failed", "occurred_at": datetime.now(timezone.utc).isoformat(),
                               "result": f"{type(exc).__name__}: {exc}"[:1000]}
            _append(path, failed)
            raise InputError("provider downgrade failed; inspect the action ledger before retrying") from exc
        completed = started | {"event": "executed", "status": "executed", "occurred_at": datetime.now(timezone.utc).isoformat(),
                               "result": str(result)[:1000]}
        _append(path, completed)
        return completed
    transitioned = state | {"event": target_event, "status": target_event, "actor": actor.strip(),
                            "occurred_at": datetime.now(timezone.utc).isoformat()}
    if event == "approved":
        transitioned["approved_by"] = actor.strip()
    _append(path, transitioned)
    return transitioned


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


@contextmanager
def _ledger_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    try:
        lock = lock_path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise InputError(f"tier action ledger is locked at {lock_path}") from exc
    try:
        yield
    finally:
        lock.close()
        lock_path.unlink(missing_ok=True)
