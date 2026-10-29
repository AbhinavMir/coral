"""Parsers for public rate files.

Provider side: CMS Hospital Price Transparency machine-readable file, CSV "tall"
layout, v2.x (45 CFR 180.50). Columns used: description, code|N, code|N|type,
payer_name, plan_name, modifiers, setting, standard_charge|gross,
standard_charge|negotiated_dollar, standard_charge|negotiated_percentage,
standard_charge|methodology.

Payer side: Transparency in Coverage in-network rate file (JSON, CMS schema).
Streamed with ijson. Items: billing_code_type, billing_code, negotiated_rates[]
with provider_references or provider_groups[].npi[] and negotiated_prices[]
{negotiated_type, negotiated_rate, billing_class, service_code[], billing_code_modifier[]}.

Both parsers yield coral.verify.Line. Identifiers for payer and plan are
normalized by the caller through a mapping file; the files do not share ids.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Iterator

import ijson

from .verify import Line
from .typed import SETTING


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


def _setting(s: str) -> int | None:
    s = (s or "").strip().lower()
    if s in ("inpatient", "outpatient", "professional"):
        return SETTING[s]
    if s == "both":
        return None
    return None


def provider_header(path: Path) -> dict[str, str]:
    """The two-row header block of a CMS v2 tall CSV: provider name, last_updated_on, version, type_2_npi, ..."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO("\n".join(text.splitlines()[:2]))))
    return dict(zip(rows[0], rows[1]))


def provider_csv(path: Path, provider_id: str, npi: str, payer_ids: dict[str, str],
                 plans: set[str] | None = None) -> Iterator[Line]:
    """Yield one Line per negotiated row of a CMS v2 tall CSV.

    payer_ids: payer_name as written in the file -> payerId on the ledger. The plan name in the
    file is the planId. Rows whose payer is not in the map, or whose plan is not in `plans`
    when given, are skipped.
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    # v2 files have a 3-row header block; the column header is the row that contains "code|1".
    start = next(i for i, ln in enumerate(lines[:10]) if "code|1" in ln)
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    for row in reader:
        payer_name, plan_id = row.get("payer_name", "").strip(), row.get("plan_name", "").strip()
        if payer_name not in payer_ids or (plans is not None and plan_id not in plans):
            continue
        payer_id = payer_ids[payer_name]
        code, ctype = None, None
        for n in range(1, 5):
            c = row.get(f"code|{n}", "").strip()
            t = row.get(f"code|{n}|type", "").strip().upper()
            if c and t in ("CPT", "HCPCS", "MS-DRG", "DRG", "APR-DRG"):
                code, ctype = c, t
                break
        if not code:
            continue
        setting = _setting(row.get("setting", ""))
        if setting is None:
            for s in (1, 2):
                yield _provider_line(row, provider_id, npi, payer_id, plan_id, f"{ctype}:{code}", s)
        else:
            yield _provider_line(row, provider_id, npi, payer_id, plan_id, f"{ctype}:{code}", setting)


def _provider_line(row, provider_id, npi, payer_id, plan_id, code, setting) -> Line:
    mods = (row.get("modifiers") or "").strip().replace(" ", "")
    return Line(
        source="provider", payerId=payer_id, planId=plan_id, providerId=provider_id, npi=npi,
        code=code, modifier_=mods, setting=setting,
        amount_cents=cents(row.get("standard_charge|negotiated_dollar")),
        percent_bp=bp(row.get("standard_charge|negotiated_percentage")),
        gross_charge_cents=cents(row.get("standard_charge|gross")),
    )


def payer_json(path: Path, payer_id: str, plan_id: str, provider_map: dict[str, str], npis: set[str] | None = None) -> Iterator[Line]:
    """Yield one Line per negotiated price for NPIs of interest from a TiC in-network file.

    provider_map: NPI -> providerId (TIN) on the ledger. Only NPIs in the map are kept.
    Files with provider_references are resolved from the top-level provider_references array
    on a first pass.
    """
    refs = {}
    with path.open("rb") as f:
        for ref in ijson.items(f, "provider_references.item"):
            group_npis = []
            for g in ref.get("provider_groups", []):
                group_npis += [str(n) for n in g.get("npi", [])]
            refs[ref["provider_group_id"]] = group_npis
    with path.open("rb") as f:
        for item in ijson.items(f, "in_network.item"):
            code = f"{item.get('billing_code_type', '').upper()}:{item.get('billing_code', '')}"
            for nr in item.get("negotiated_rates", []):
                group_npis = []
                for g in nr.get("provider_groups", []):
                    group_npis += [str(n) for n in g.get("npi", [])]
                for pr in nr.get("provider_references", []):
                    group_npis += refs.get(pr, [])
                group_npis = [n for n in group_npis if n in provider_map and (npis is None or n in npis)]
                if not group_npis:
                    continue
                for price in nr.get("negotiated_prices", []):
                    bc = (price.get("billing_class") or "").lower()
                    setting = SETTING["professional"] if bc == "professional" else None
                    if setting is None:
                        codes = set(price.get("service_code") or [])
                        # place-of-service 21 = inpatient, 22 = on-campus outpatient, 19 = off-campus outpatient
                        if codes & {"21"}:
                            setting = SETTING["inpatient"]
                        elif codes & {"22", "19", "24"}:
                            setting = SETTING["outpatient"]
                        else:
                            setting = SETTING["outpatient"]
                    ntype = (price.get("negotiated_type") or "").lower()
                    rate = price.get("negotiated_rate")
                    mods = ",".join(price.get("billing_code_modifier") or [])
                    for n in group_npis:
                        yield Line(
                            source="payer", payerId=payer_id, planId=plan_id, providerId=provider_map[n], npi=n,
                            code=code, modifier_=mods, setting=setting,
                            amount_cents=cents(rate) if ntype in ("negotiated", "fee schedule", "derived") else None,
                            percent_bp=bp(rate) if ntype == "percentage" else None,
                        )


def gross_charges(path: Path) -> dict[tuple[str, int], int]:
    """(code, setting) -> list price in cents, from the standard_charge|gross column of a CMS v2 tall CSV.

    The gross charge does not depend on the payer, so the first non-empty value per code and
    setting is taken. Rows with setting "both" fill both settings.
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines[:10]) if "code|1" in ln)
    out: dict[tuple[str, int], int] = {}
    for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        g = cents(row.get("standard_charge|gross"))
        if g is None:
            continue
        code = None
        for n in range(1, 5):
            c = row.get(f"code|{n}", "").strip()
            t = row.get(f"code|{n}|type", "").strip().upper()
            if c and t in ("CPT", "HCPCS", "MS-DRG", "DRG", "APR-DRG"):
                code = f"{t}:{c}"
                break
        if not code:
            continue
        setting = _setting(row.get("setting", ""))
        for st in ([1, 2] if setting is None else [setting]):
            out.setdefault((code, st), g)
    return out
