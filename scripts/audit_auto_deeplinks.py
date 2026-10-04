"""Audit the auto-action deeplink invariant across every cached plan.

    python -m scripts.audit_auto_deeplinks [--cache-version N] [--apply]

Reports, per cached plan, how many actions are categorised `auto` and how many of those
carry a deeplink that is actually in the catalog. This is the metric the benchmark reports
as "Auto actions carrying valid actionable deeplink".

With --apply it also repairs stored plans in place, performing exactly the downgrade the
pipeline now performs at generation time: an `auto` action whose resolution produced no
catalog destination becomes `manual`, keeping its grounded steps. Nothing else changes, no
URI is invented, and `critical` is never touched.

The repair exists because the semantic cache holds plans generated before the rule, and
those now fail re-validation on read. Without it every such plan would be discarded and
regenerated, which costs a model call per plan; with it the stored plan is corrected
deterministically, because the downgrade depends only on whether a catalog destination was
found, never on the model.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field

from app.bootstrap import load_catalog_state
from app.config import get_settings
from app.contract.schema import ContextDeeplinkResponse, actionCategory
from app.db.session import connect
from app.validation.rules import validate_plan


@dataclass
class Tally:
    plans: int = 0
    auto_actions: int = 0
    auto_linked: int = 0
    downgraded: int = 0
    plans_changed: int = 0
    invalid_before: int = 0
    invalid_after: int = 0
    offenders: list[str] = field(default_factory=list)

    @property
    def pct(self) -> str:
        if not self.auto_actions:
            return "n/a (no auto actions)"
        return f"{self.auto_linked / self.auto_actions * 100:.1f}% ({self.auto_linked}/{self.auto_actions})"


def audit(cache_version: int | None, apply: bool) -> Tally:
    settings = get_settings()
    _, _, permitted = load_catalog_state(settings)

    tally = Tally()

    with connect(settings) as conn:
        if cache_version is None:
            rows = conn.execute(
                "SELECT id, cache_version, plan FROM plan_cache ORDER BY id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, cache_version, plan FROM plan_cache "
                "WHERE cache_version = %s ORDER BY id",
                (cache_version,),
            ).fetchall()

        for plan_id, version, raw in rows:
            payload = json.loads(raw) if isinstance(raw, str) else raw
            plan = ContextDeeplinkResponse.model_validate(payload)
            tally.plans += 1

            if not validate_plan(plan, permitted).ok:
                tally.invalid_before += 1

            changed = False
            for goal in plan.contexts:
                for action in goal.actions:
                    if action.category != actionCategory.auto:
                        continue

                    tally.auto_actions += 1
                    linked = any(
                        g.actionableDeeplink is not None
                        and g.actionableDeeplink.deeplink in permitted
                        for g in action.stepGroups
                    )
                    if linked:
                        tally.auto_linked += 1
                        continue

                    tally.offenders.append(f"plan {plan_id} (v{version}): {action.actionName}")
                    tally.downgraded += 1
                    if apply:
                        # The same repair the pipeline performs: the steps stay, only the
                        # promise about how the action is carried out changes.
                        action.category = actionCategory.manual
                        changed = True

            if apply and changed:
                tally.plans_changed += 1
                with conn.transaction():
                    conn.execute(
                        "UPDATE plan_cache SET plan = %s, validated_at = now() "
                        "WHERE id = %s",
                        (json.dumps(plan.model_dump(mode="json")), plan_id),
                    )

            if not validate_plan(plan, permitted).ok:
                tally.invalid_after += 1

    return tally


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-version", type=int, default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="repair stored plans in place by downgrading unlinked auto actions",
    )
    args = parser.parse_args()

    tally = audit(args.cache_version, args.apply)

    print(f"plans examined                 : {tally.plans}")
    print(f"auto actions                   : {tally.auto_actions}")
    print(f"auto actions with catalog link : {tally.auto_linked}")
    print(f"auto actions without one       : {tally.downgraded}")
    print(f"metric                         : {tally.pct}")
    print(f"plans failing validation before: {tally.invalid_before}")
    print(f"plans failing validation after : {tally.invalid_after}")
    if args.apply:
        print(f"plans repaired                 : {tally.plans_changed}")

    if tally.offenders:
        print("\nauto actions with no catalog destination:")
        for line in tally.offenders:
            print(f"  - {line}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
