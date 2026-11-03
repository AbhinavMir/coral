"""Parsers for public rate files.

Provider side: CMS Hospital Price Transparency machine-readable file, CSV "tall"
layout, v2.x and v3.0 (45 CFR 180.50). Columns used: code|N, code|N|type,
modifiers, setting, drug_unit_of_measurement, drug_type_of_measurement,
billing_class, payer_name, plan_name, standard_charge|gross,
standard_charge|negotiated_dollar, standard_charge|negotiated_percentage,
standard_charge|negotiated_algorithm, standard_charge|methodology.

Payer side: Transparency in Coverage in-network rate file (JSON, CMS schema).
Streamed with ijson. Items: billing_code_type, billing_code, negotiated_rates[]
with provider_references or provider_groups[].npi[] and negotiated_prices[]
{negotiated_type, negotiated_rate, billing_class, service_code[], billing_code_modifier[]}.

Both parsers yield coral.verify.Line. Identifiers for payer and plan are
normalized by the caller through a mapping; the files do not share ids.
Lines the parser cannot key are counted in a Skipped object, not guessed.
"""
from __future__ import annotations

import csv
import gzip
import io
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import ijson

from .verify import Line
from .typed import SETTING, BILLING_CLASS

CODE_TYPES = ("CPT", "HCPCS", "MS-DRG", "DRG", "APR-DRG")

# TiC place-of-service codes that fix the setting. 21 inpatient hospital;
# 19 off-campus outpatient, 22 on-campus outpatient, 23 emergency room, 24 ambulatory surgical center.
POS_INPATIENT = {"21"}
POS_OUTPATIENT = {"19", "22", "23", "24"}


@dataclass
class Skipped:
    """Why lines were dropped. Reported with every run."""
    counts: Counter = field(default_factory=Counter)

    def add(self, reason: str):
        self.counts[reason] += 1


def cents(x) -> int | None:
    if x in (None, "", "N/A"):
        return None
    try:
        return round(float(str(x).replace(",", "").replace("$", "")) * 100)
    except ValueError:
        return None


def bp(x) -> int | None:
    if x in (None, "", "N/A"):
        return None
    try:
        return round(float(x) * 100)
    except ValueError:
        return None


def normalize_modifiers(text: str | None) -> str:
    """Comma-joined, sorted, upper-case modifier list; the same on both sides."""
    parts = [p.strip().upper() for p in (text or "").replace(";", ",").replace(" ", ",").split(",")]
    return ",".join(sorted(p for p in parts if p))


def _setting(s: str) -> int | None:
    s = (s or "").strip().lower()
    if s in ("inpatient", "outpatient"):
        return SETTING[s]
    return None   # "both" or empty: the caller emits one line per setting


def _code(row) -> str | None:
    for n in range(1, 5):
        c = row.get(f"code|{n}", "").strip()
        t = row.get(f"code|{n}|type", "").strip().upper()
        if c and t in CODE_TYPES:
            return f"{t}:{c}"
    return None


def _unit(row) -> str:
    amount = (row.get("drug_unit_of_measurement") or "").strip()
    kind = (row.get("drug_type_of_measurement") or "").strip().upper()
    if not amount and not kind:
        return ""
    return f"{amount} {kind}".strip()


def _rows(path: Path):
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    # v2/v3 files have a 3-row header block; the column header is the row that contains "code|1".
    start = next(i for i, ln in enumerate(lines[:10]) if "code|1" in ln)
    return csv.DictReader(io.StringIO("\n".join(lines[start:])))


def provider_header(path: Path) -> dict[str, str]:
    """The two-row header block of a CMS tall CSV: provider name, last_updated_on, version, type_2_npi, ..."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO("\n".join(text.splitlines()[:2]))))
    return dict(zip(rows[0], rows[1]))


def provider_csv(path: Path, provider_id: str, npi: str, payer_ids: dict[str, str],
                 plans: set[str] | None = None, skipped: Skipped | None = None) -> Iterator[Line]:
    """Yield one Line per negotiated row of a CMS tall CSV.

    payer_ids: payer_name as written in the file -> payerId on the ledger. The plan name in the
    file is the planId. Rows whose payer is not in the map, or whose plan is not in `plans`
    when given, are skipped. Rows with only an algorithm and no dollar or percent value carry
    nothing a record can hold; they are yielded with both amounts None so the caller can count them.
    """
    skipped = skipped if skipped is not None else Skipped()
    for row in _rows(path):
        payer_name, plan_id = row.get("payer_name", "").strip(), row.get("plan_name", "").strip()
        if payer_name not in payer_ids:
            skipped.add("payer_not_registered")
            continue
        if plans is not None and plan_id not in plans:
            skipped.add("plan_not_selected")
            continue
        code = _code(row)
        if not code:
            skipped.add("no_billing_code")
            continue
        bc = (row.get("billing_class") or "facility").strip().lower()
        if bc not in BILLING_CLASS:
            skipped.add("billing_class_unknown")
            continue
        setting = _setting(row.get("setting", ""))
        for s in ([1, 2] if setting is None else [setting]):
            yield _provider_line(row, provider_id, npi, payer_ids[payer_name], plan_id, code, s, BILLING_CLASS[bc])


def _provider_line(row, provider_id, npi, payer_id, plan_id, code, setting, billing_class) -> Line:
    return Line(
        source="provider", payerId=payer_id, planId=plan_id, providerId=provider_id, npi=npi,
        code=code, modifier_=normalize_modifiers(row.get("modifiers")), setting=setting,
        billingClass=billing_class, unit=_unit(row),
        amount_cents=cents(row.get("standard_charge|negotiated_dollar")),
        percent_bp=bp(row.get("standard_charge|negotiated_percentage")),
        gross_charge_cents=cents(row.get("standard_charge|gross")),
    )


def payer_json(path: Path, payer_id: str, plan_id: str, provider_map: dict[str, str],
               npis: set[str] | None = None, skipped: Skipped | None = None,
               codes_of_interest: set[str] | None = None) -> Iterator[Line]:
    """Yield one Line per negotiated price for NPIs of interest from a TiC in-network file.

    provider_map: NPI -> providerId (TIN) on the ledger. Only NPIs in the map are kept.
    provider_references are resolved from the top-level provider_references array on a first
    pass. A reference served by a `location` URL instead of inline groups is not fetched; prices
    that point only to such references are counted as provider_reference_remote and dropped.
    A price with no place of service (service_code) has no setting and is counted as
    setting_unknown; the parser does not guess outpatient.
    """
    skipped = skipped if skipped is not None else Skipped()
    opener = gzip.open if path.suffix == ".gz" else open
    refs: dict = {}
    remote_refs: set = set()
    with opener(path, "rb") as f:
        for ref in ijson.items(f, "provider_references.item"):
            if "location" in ref and not ref.get("provider_groups"):
                remote_refs.add(ref["provider_group_id"])
                continue
            group_npis = []
            for g in ref.get("provider_groups", []):
                group_npis += [str(n) for n in g.get("npi", [])]
            refs[ref["provider_group_id"]] = group_npis
    with opener(path, "rb") as f:
        for item in ijson.items(f, "in_network.item"):
            if codes_of_interest is not None and f"{item.get('billing_code_type', '').upper()}:{item.get('billing_code', '')}" not in codes_of_interest:
                continue
            code = f"{item.get('billing_code_type', '').upper()}:{item.get('billing_code', '')}"
            for nr in item.get("negotiated_rates", []):
                group_npis = []
                remote_only = False
                for g in nr.get("provider_groups", []):
                    group_npis += [str(n) for n in g.get("npi", [])]
                for pr in nr.get("provider_references", []):
                    if pr in remote_refs:
                        remote_only = True
                    group_npis += refs.get(pr, [])
                group_npis = sorted({n for n in group_npis if n in provider_map and (npis is None or n in npis)})
                if not group_npis:
                    if remote_only:
                        skipped.add("provider_reference_remote")
                    continue
                for price in nr.get("negotiated_prices", []):
                    bc = (price.get("billing_class") or "").lower()
                    if bc not in BILLING_CLASS:
                        skipped.add("billing_class_unknown")
                        continue
                    codes = set(price.get("service_code") or [])
                    if codes & POS_INPATIENT and not codes & POS_OUTPATIENT:
                        setting = SETTING["inpatient"]
                    elif codes & POS_OUTPATIENT and not codes & POS_INPATIENT:
                        setting = SETTING["outpatient"]
                    elif codes & POS_INPATIENT:
                        setting = None   # both: emit one line per setting
                    else:
                        skipped.add("setting_unknown")
                        continue
                    ntype = (price.get("negotiated_type") or "").lower()
                    rate = price.get("negotiated_rate")
                    mods = normalize_modifiers(",".join(price.get("billing_code_modifier") or []))
                    for n in group_npis:
                        for s in ([1, 2] if setting is None else [setting]):
                            yield Line(
                                source="payer", payerId=payer_id, planId=plan_id, providerId=provider_map[n], npi=n,
                                code=code, modifier_=mods, setting=s, billingClass=BILLING_CLASS[bc], unit="",
                                amount_cents=cents(rate) if ntype in ("negotiated", "fee schedule", "derived", "per diem") else None,
                                percent_bp=bp(rate) if ntype == "percentage" else None,
                            )


def gross_charges(path: Path) -> dict[tuple[str, int, str], int]:
    """(code, setting, unit) -> list price in cents, from the standard_charge|gross column.

    The gross charge does not depend on the payer, so the first non-empty value per code,
    setting, and unit is taken. Rows with setting "both" fill both settings.
    """
    out: dict[tuple[str, int, str], int] = {}
    for row in _rows(path):
        g = cents(row.get("standard_charge|gross"))
        if g is None:
            continue
        code = _code(row)
        if not code:
            continue
        setting = _setting(row.get("setting", ""))
        unit = _unit(row)
        for st in ([1, 2] if setting is None else [setting]):
            out.setdefault((code, st, unit), g)
    return out
