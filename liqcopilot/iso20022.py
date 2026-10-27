"""
ISO 20022 RTGS XML Parser, Validator, and Generator.

Supports:
- pacs.008.001.08 (FIToFICstmrCdtTrf - Customer Credit Transfer)
- pacs.009.001.08 (FICdtTrf - Financial Institution Credit Transfer)
- camt.054.001.08 (BkToCstmrDbtCdtNtfctn - Bank to Customer Debit/Credit Notification)
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring

NAMESPACES: Dict[str, str] = {
    "pacs.008.001.08": "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08",
    "pacs.009.001.08": "urn:iso:std:iso:20022:tech:xsd:pacs.009.001.08",
    "camt.054.001.08": "urn:iso:std:iso:20022:tech:xsd:camt.054.001.08",
}

VALID_PRIORITIES = {"URGT", "HIGH", "NORM"}
VALID_MSG_TYPES = set(NAMESPACES.keys())

# SWIFT BIC (8 or 11 characters: 4 bank + 2 country + 2 location + optional 3 branch)
BIC_REGEX = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")
# Indian RTGS/NEFT IFSC (11 characters: 4 alpha bank code + '0' + 6 alphanumeric branch code)
IFSC_REGEX = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
# ActiveCurrencyAndAmount: at most 18 digits, 5 of them after the point; no sign, exponent, NaN or inf
AMOUNT_REGEX = re.compile(r"^\d{1,13}(\.\d{1,5})?$")
CURRENCY_REGEX = re.compile(r"^[A-Z]{3}$")
MAX35 = 35  # Max35Text: MsgId, EndToEndId, InstrId, TxId
MAX_XML_CHARS = 200_000


class ISO20022ValidationError(ValueError):
    """Raised when an ISO 20022 message or field fails structural or domain validation."""


@dataclass
class ISO20022Payment:
    """Normalized representation of an ISO 20022 RTGS payment or settlement notification."""

    msg_id: str
    end_to_end_id: str
    debtor_agent: str
    creditor_agent: str
    amount: Decimal
    currency: str = "INR"
    priority: str = "NORM"
    settlement_timestamp: str = "2026-10-22T09:15:00Z"
    msg_type: str = "pacs.008.001.08"
    settlement_method: str = "CLRG"
    settlement_system: str = "RTGS"
    credit_debit_indicator: str = "CRDT"
    status: str = "BOOK"
    debtor_name: Optional[str] = None
    creditor_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def validate_bic_or_ifsc(code: str) -> str:
    """Validate that an agent identifier is a standards-compliant SWIFT BIC or Indian IFSC."""
    if not code or not isinstance(code, str):
        raise ISO20022ValidationError("Agent BIC/IFSC must be a non-empty string.")
    normalized = code.strip().upper()
    if BIC_REGEX.match(normalized) or IFSC_REGEX.match(normalized):
        return normalized
    raise ISO20022ValidationError(
        f"Invalid BIC/IFSC format '{code}'. Expected 8/11-char SWIFT BIC (e.g. SBININBBXXX) "
        f"or 11-char Indian IFSC (e.g. SBIN0000001)."
    )


def validate_iso20022_payment(payment: ISO20022Payment) -> None:
    """Validate all fields of an ISO20022Payment object."""
    if payment.msg_type not in VALID_MSG_TYPES:
        raise ISO20022ValidationError(
            f"Unsupported msg_type '{payment.msg_type}'. Must be one of {sorted(VALID_MSG_TYPES)}."
        )
    if not payment.msg_id or not payment.msg_id.strip() or len(payment.msg_id) > MAX35:
        raise ISO20022ValidationError("MsgId must be 1-35 characters.")
    if not payment.end_to_end_id or not payment.end_to_end_id.strip() or len(payment.end_to_end_id) > MAX35:
        raise ISO20022ValidationError("EndToEndId must be 1-35 characters.")

    debtor = validate_bic_or_ifsc(payment.debtor_agent)
    creditor = validate_bic_or_ifsc(payment.creditor_agent)
    if debtor == creditor:
        raise ISO20022ValidationError(
            f"Debtor agent and creditor agent cannot be identical ('{debtor}')."
        )
    payment.debtor_agent = debtor
    payment.creditor_agent = creditor

    try:
        payment.amount = as_money(payment.amount)
    except ValueError as exc:
        raise ISO20022ValidationError(str(exc)) from None
    if payment.amount <= 0:
        raise ISO20022ValidationError(
            f"Settlement amount must be strictly positive (got {payment.amount})."
        )

    currency = (payment.currency or "").strip().upper()
    if not CURRENCY_REGEX.match(currency):
        raise ISO20022ValidationError(
            f"Currency must be a 3-letter ISO 4217 code (got '{payment.currency}')."
        )
    payment.currency = currency

    priority = payment.priority.strip().upper() if payment.priority else ""
    if priority not in VALID_PRIORITIES:
        raise ISO20022ValidationError(
            f"Invalid priority '{payment.priority}'. Expected one of {sorted(VALID_PRIORITIES)}."
        )
    payment.priority = priority

    try:
        datetime.fromisoformat(payment.settlement_timestamp.strip())
    except (AttributeError, ValueError) as exc:
        raise ISO20022ValidationError(
            f"Settlement timestamp must be an ISO 8601 date-time (got '{payment.settlement_timestamp}')."
        ) from exc


def as_money(value: Any) -> Decimal:
    """An exact, finite decimal amount. Money is never float here: JSON numbers and other
    floats go through str(), so 0.1 stays 0.1 rather than its binary expansion."""
    if isinstance(value, bool):
        raise ValueError("amount must be a number, not a boolean")
    if isinstance(value, Decimal):
        d = value
    else:
        try:
            d = Decimal(str(value).strip())
        except InvalidOperation:
            raise ValueError("amount must be a decimal number") from None
    if not d.is_finite():
        raise ValueError("amount must be a finite number")
    return d


def _parse_amount(elem: ET.Element) -> tuple[Decimal, str]:
    """Read an ActiveCurrencyAndAmount element; the Ccy attribute is mandatory."""
    text = (elem.text or "").strip()
    if not AMOUNT_REGEX.match(text):
        raise ISO20022ValidationError(f"Invalid amount '{elem.text}': expected digits with up to 5 decimals.")
    currency = elem.attrib.get("Ccy")
    if not currency:
        raise ISO20022ValidationError(f"Amount element {_strip_ns(elem.tag)} is missing its Ccy attribute.")
    return Decimal(text), currency


def _strip_ns(tag: str) -> str:
    """Strip XML namespace URI from an ElementTree tag."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _extract_ns(tag: str) -> str:
    """Extract XML namespace URI from an ElementTree root tag."""
    if tag.startswith("{") and "}" in tag:
        return tag[1 : tag.index("}")]
    return ""


def _find_first_text(elem: ET.Element, local_path: list[str]) -> Optional[str]:
    """Find text of a nested child path matching local tag names regardless of namespace."""
    curr = elem
    for step in local_path:
        found = None
        for child in curr:
            if _strip_ns(child.tag) == step:
                found = child
                break
        if found is None:
            return None
        curr = found
    return curr.text.strip() if curr.text else None


def _find_first_elem(elem: ET.Element, local_path: list[str]) -> Optional[ET.Element]:
    """Find a nested Element matching local tag names regardless of namespace."""
    curr = elem
    for step in local_path:
        found = None
        for child in curr:
            if _strip_ns(child.tag) == step:
                found = child
                break
        if found is None:
            return None
        curr = found
    return curr


def _extract_agent_code(agent_elem: Optional[ET.Element]) -> Optional[str]:
    """Extract BICFI or ClrSysMmbId/MmbId from FinInstnId inside DbtrAgt/CdtrAgt."""
    if agent_elem is None:
        return None
    bic = _find_first_text(agent_elem, ["FinInstnId", "BICFI"])
    if bic:
        return bic
    mmb_id = _find_first_text(
        agent_elem, ["FinInstnId", "ClrSysMmbId", "MmbId"]
    )
    if mmb_id:
        return mmb_id
    return None


def _populate_agent_elem(parent: ET.Element, tag_name: str, agent_code: str) -> ET.Element:
    """Populate DbtrAgt or CdtrAgt with BICFI or ClrSysMmbId (for IFSC)."""
    agt = ET.SubElement(parent, tag_name)
    fin_id = ET.SubElement(agt, "FinInstnId")
    code = validate_bic_or_ifsc(agent_code)
    if IFSC_REGEX.match(code):
        clr = ET.SubElement(fin_id, "ClrSysMmbId")
        clr_id = ET.SubElement(clr, "ClrSysId")
        cd = ET.SubElement(clr_id, "Cd")
        cd.text = "INFSC"
        mmb = ET.SubElement(clr, "MmbId")
        mmb.text = code
    else:
        bic_el = ET.SubElement(fin_id, "BICFI")
        bic_el.text = code
    return agt


def _add_clearing_system(sttlm_inf: ET.Element, system: str) -> None:
    """ClrSys/Cd only takes codes of up to 3 characters (e.g. TGT); longer names go in Prtry."""
    clr_sys = ET.SubElement(sttlm_inf, "ClrSys")
    ET.SubElement(clr_sys, "Cd" if len(system) <= 3 else "Prtry").text = system


def _add_priorities(pmt_tp: ET.Element, priority: str) -> None:
    """PmtTpInf/InstrPrty is Priority2Code (HIGH or NORM); URGT is only valid in SttlmPrty."""
    ET.SubElement(pmt_tp, "InstrPrty").text = "NORM" if priority == "NORM" else "HIGH"


def generate_pacs008_xml(payment: ISO20022Payment) -> str:
    """Generate standards-compliant ISO 20022 pacs.008.001.08 (FIToFICstmrCdtTrf) XML."""
    payment.msg_type = "pacs.008.001.08"
    validate_iso20022_payment(payment)

    ns = NAMESPACES["pacs.008.001.08"]
    ET.register_namespace("", ns)
    doc = ET.Element("Document", xmlns=ns)
    root = ET.SubElement(doc, "FIToFICstmrCdtTrf")

    grp_hdr = ET.SubElement(root, "GrpHdr")
    ET.SubElement(grp_hdr, "MsgId").text = payment.msg_id
    ET.SubElement(grp_hdr, "CreDtTm").text = payment.settlement_timestamp
    ET.SubElement(grp_hdr, "NbOfTxs").text = "1"
    sttlm_inf = ET.SubElement(grp_hdr, "SttlmInf")
    ET.SubElement(sttlm_inf, "SttlmMtd").text = payment.settlement_method
    _add_clearing_system(sttlm_inf, payment.settlement_system)

    tx = ET.SubElement(root, "CdtTrfTxInf")
    pmt_id = ET.SubElement(tx, "PmtId")
    ET.SubElement(pmt_id, "InstrId").text = payment.msg_id
    ET.SubElement(pmt_id, "EndToEndId").text = payment.end_to_end_id
    ET.SubElement(pmt_id, "TxId").text = payment.end_to_end_id

    pmt_tp = ET.SubElement(tx, "PmtTpInf")
    _add_priorities(pmt_tp, payment.priority)
    svc_lvl = ET.SubElement(pmt_tp, "SvcLvl")
    ET.SubElement(svc_lvl, "Cd").text = "URGP" if payment.priority == "URGT" else "NURG"

    amt_el = ET.SubElement(tx, "IntrBkSttlmAmt", Ccy=payment.currency)
    amt_el.text = f"{payment.amount:.2f}"
    ET.SubElement(tx, "IntrBkSttlmDt").text = payment.settlement_timestamp[:10]
    ET.SubElement(tx, "SttlmPrty").text = payment.priority
    ET.SubElement(tx, "ChrgBr").text = "SHAR"

    dbtr = ET.SubElement(tx, "Dbtr")
    ET.SubElement(dbtr, "Nm").text = payment.debtor_name or f"Treasury-{payment.debtor_agent}"
    _populate_agent_elem(tx, "DbtrAgt", payment.debtor_agent)
    _populate_agent_elem(tx, "CdtrAgt", payment.creditor_agent)
    cdtr = ET.SubElement(tx, "Cdtr")
    ET.SubElement(cdtr, "Nm").text = payment.creditor_name or f"Treasury-{payment.creditor_agent}"

    xml_bytes = ET.tostring(doc, encoding="utf-8", xml_declaration=True)
    return xml_bytes.decode("utf-8")


def generate_pacs009_xml(payment: ISO20022Payment) -> str:
    """Generate standards-compliant ISO 20022 pacs.009.001.08 (FICdtTrf) XML."""
    payment.msg_type = "pacs.009.001.08"
    validate_iso20022_payment(payment)

    ns = NAMESPACES["pacs.009.001.08"]
    ET.register_namespace("", ns)
    doc = ET.Element("Document", xmlns=ns)
    root = ET.SubElement(doc, "FICdtTrf")

    grp_hdr = ET.SubElement(root, "GrpHdr")
    ET.SubElement(grp_hdr, "MsgId").text = payment.msg_id
    ET.SubElement(grp_hdr, "CreDtTm").text = payment.settlement_timestamp
    ET.SubElement(grp_hdr, "NbOfTxs").text = "1"
    sttlm_inf = ET.SubElement(grp_hdr, "SttlmInf")
    ET.SubElement(sttlm_inf, "SttlmMtd").text = payment.settlement_method
    _add_clearing_system(sttlm_inf, payment.settlement_system)

    tx = ET.SubElement(root, "CdtTrfTxInf")
    pmt_id = ET.SubElement(tx, "PmtId")
    ET.SubElement(pmt_id, "InstrId").text = payment.msg_id
    ET.SubElement(pmt_id, "EndToEndId").text = payment.end_to_end_id
    ET.SubElement(pmt_id, "TxId").text = payment.end_to_end_id

    pmt_tp = ET.SubElement(tx, "PmtTpInf")
    _add_priorities(pmt_tp, payment.priority)

    amt_el = ET.SubElement(tx, "IntrBkSttlmAmt", Ccy=payment.currency)
    amt_el.text = f"{payment.amount:.2f}"
    ET.SubElement(tx, "IntrBkSttlmDt").text = payment.settlement_timestamp[:10]
    ET.SubElement(tx, "SttlmPrty").text = payment.priority

    _populate_agent_elem(tx, "InstgAgt", payment.debtor_agent)
    _populate_agent_elem(tx, "InstdAgt", payment.creditor_agent)
    _populate_agent_elem(tx, "Dbtr", payment.debtor_agent)
    _populate_agent_elem(tx, "DbtrAgt", payment.debtor_agent)
    _populate_agent_elem(tx, "CdtrAgt", payment.creditor_agent)
    _populate_agent_elem(tx, "Cdtr", payment.creditor_agent)

    xml_bytes = ET.tostring(doc, encoding="utf-8", xml_declaration=True)
    return xml_bytes.decode("utf-8")


def generate_camt054_xml(
    payment: ISO20022Payment,
    credit_debit: str = "CRDT",
    status: str = "BOOK",
) -> str:
    """Generate standards-compliant ISO 20022 camt.054.001.08 (BkToCstmrDbtCdtNtfctn) XML."""
    payment.msg_type = "camt.054.001.08"
    payment.credit_debit_indicator = credit_debit
    payment.status = status
    validate_iso20022_payment(payment)

    ns = NAMESPACES["camt.054.001.08"]
    ET.register_namespace("", ns)
    doc = ET.Element("Document", xmlns=ns)
    root = ET.SubElement(doc, "BkToCstmrDbtCdtNtfctn")

    grp_hdr = ET.SubElement(root, "GrpHdr")
    ET.SubElement(grp_hdr, "MsgId").text = payment.msg_id
    ET.SubElement(grp_hdr, "CreDtTm").text = payment.settlement_timestamp

    ntfctn = ET.SubElement(root, "Ntfctn")
    ET.SubElement(ntfctn, "Id").text = f"NTF-{payment.msg_id}"[:MAX35]
    ET.SubElement(ntfctn, "CreDtTm").text = payment.settlement_timestamp

    acct = ET.SubElement(ntfctn, "Acct")
    acct_id = ET.SubElement(acct, "Id")
    othr = ET.SubElement(acct_id, "Othr")
    ET.SubElement(othr, "Id").text = f"RTGS-SETTLM-{payment.creditor_agent}"
    ET.SubElement(acct, "Ccy").text = payment.currency

    ntry = ET.SubElement(ntfctn, "Ntry")
    amt_el = ET.SubElement(ntry, "Amt", Ccy=payment.currency)
    amt_el.text = f"{payment.amount:.2f}"
    ET.SubElement(ntry, "CdtDbtInd").text = credit_debit
    sts_el = ET.SubElement(ntry, "Sts")
    ET.SubElement(sts_el, "Cd").text = status

    bookg_dt = ET.SubElement(ntry, "BookgDt")
    ET.SubElement(bookg_dt, "DtTm").text = payment.settlement_timestamp
    # BkTxCd is mandatory in a camt.054 entry
    bk_tx_cd = ET.SubElement(ntry, "BkTxCd")
    ET.SubElement(ET.SubElement(bk_tx_cd, "Prtry"), "Cd").text = "RTGS-SETTLEMENT"

    ntry_dtls = ET.SubElement(ntry, "NtryDtls")
    tx_dtls = ET.SubElement(ntry_dtls, "TxDtls")
    refs = ET.SubElement(tx_dtls, "Refs")
    ET.SubElement(refs, "MsgId").text = payment.msg_id
    ET.SubElement(refs, "EndToEndId").text = payment.end_to_end_id

    rltd_agts = ET.SubElement(tx_dtls, "RltdAgts")
    _populate_agent_elem(rltd_agts, "DbtrAgt", payment.debtor_agent)
    _populate_agent_elem(rltd_agts, "CdtrAgt", payment.creditor_agent)

    xml_bytes = ET.tostring(doc, encoding="utf-8", xml_declaration=True)
    return xml_bytes.decode("utf-8")


def generate_iso20022_xml(payment: ISO20022Payment) -> str:
    """Dispatch XML generation based on payment.msg_type."""
    if payment.msg_type == "pacs.008.001.08":
        return generate_pacs008_xml(payment)
    if payment.msg_type == "pacs.009.001.08":
        return generate_pacs009_xml(payment)
    if payment.msg_type == "camt.054.001.08":
        return generate_camt054_xml(
            payment,
            credit_debit=payment.credit_debit_indicator,
            status=payment.status,
        )
    raise ISO20022ValidationError(f"Unsupported msg_type '{payment.msg_type}'.")


def parse_iso20022_xml(xml_str: str) -> ISO20022Payment:
    """Parse and validate an ISO 20022 XML document (pacs.008, pacs.009, or camt.054)."""
    if not xml_str or not isinstance(xml_str, str) or not xml_str.strip():
        raise ISO20022ValidationError("Empty XML string provided.")
    if len(xml_str) > MAX_XML_CHARS:
        raise ISO20022ValidationError(f"XML document is over {MAX_XML_CHARS} characters.")
    # ISO 20022 messages never carry a DTD; refusing one rules out entity-expansion tricks
    if "<!DOCTYPE" in xml_str or "<!ENTITY" in xml_str:
        raise ISO20022ValidationError("DOCTYPE and ENTITY declarations are not allowed.")

    try:
        doc = safe_fromstring(xml_str.strip())  # defusedxml: no entities, DTDs or external fetches
    except ET.ParseError as exc:
        raise ISO20022ValidationError(f"Malformed XML document: {exc}") from exc
    except DefusedXmlException:
        raise ISO20022ValidationError("DOCTYPE and ENTITY declarations are not allowed.") from None

    if _strip_ns(doc.tag) != "Document":
        raise ISO20022ValidationError(
            f"Root element must be 'Document', got '{_strip_ns(doc.tag)}'."
        )

    ns = _extract_ns(doc.tag)
    if len(doc) == 0:
        raise ISO20022ValidationError("Document element contains no child message node.")

    msg_root = doc[0]
    root_name = _strip_ns(msg_root.tag)

    if root_name in ("FIToFICstmrCdtTrf", "FICdtTrf"):
        expected_type = (
            "pacs.008.001.08" if root_name == "FIToFICstmrCdtTrf" else "pacs.009.001.08"
        )
        if ns and ns != NAMESPACES[expected_type]:
            raise ISO20022ValidationError(
                f"Namespace mismatch for {root_name}: got '{ns}', expected '{NAMESPACES[expected_type]}'."
            )

        msg_id = _find_first_text(msg_root, ["GrpHdr", "MsgId"])
        cre_dt_tm = _find_first_text(msg_root, ["GrpHdr", "CreDtTm"])
        sttlm_mtd = _find_first_text(msg_root, ["GrpHdr", "SttlmInf", "SttlmMtd"]) or "CLRG"
        clr_sys = (
            _find_first_text(msg_root, ["GrpHdr", "SttlmInf", "ClrSys", "Cd"])
            or _find_first_text(msg_root, ["GrpHdr", "SttlmInf", "ClrSys", "Prtry"])
            or "RTGS"
        )

        tx = _find_first_elem(msg_root, ["CdtTrfTxInf"])
        if tx is None:
            raise ISO20022ValidationError(f"Missing CdtTrfTxInf element in {expected_type}.")

        end_to_end_id = _find_first_text(tx, ["PmtId", "EndToEndId"])
        priority = (
            _find_first_text(tx, ["SttlmPrty"])
            or _find_first_text(tx, ["PmtTpInf", "InstrPrty"])
            or "NORM"
        )

        amt_elem = _find_first_elem(tx, ["IntrBkSttlmAmt"])
        if amt_elem is None:
            raise ISO20022ValidationError(
                f"Missing IntrBkSttlmAmt element in {expected_type}."
            )
        amount, currency = _parse_amount(amt_elem)

        dbtr_agt_el = _find_first_elem(tx, ["DbtrAgt"])
        if dbtr_agt_el is None:
            dbtr_agt_el = _find_first_elem(tx, ["InstgAgt"])
        cdtr_agt_el = _find_first_elem(tx, ["CdtrAgt"])
        if cdtr_agt_el is None:
            cdtr_agt_el = _find_first_elem(tx, ["InstdAgt"])

        debtor_agent = _extract_agent_code(dbtr_agt_el)
        creditor_agent = _extract_agent_code(cdtr_agt_el)
        debtor_name = _find_first_text(tx, ["Dbtr", "Nm"])
        creditor_name = _find_first_text(tx, ["Cdtr", "Nm"])

        if not msg_id:
            raise ISO20022ValidationError("Missing GrpHdr/MsgId.")
        if not end_to_end_id:
            raise ISO20022ValidationError("Missing CdtTrfTxInf/PmtId/EndToEndId.")
        if not debtor_agent:
            raise ISO20022ValidationError("Missing DbtrAgt BICFI or ClrSysMmbId.")
        if not creditor_agent:
            raise ISO20022ValidationError("Missing CdtrAgt BICFI or ClrSysMmbId.")

        payment = ISO20022Payment(
            msg_id=msg_id,
            end_to_end_id=end_to_end_id,
            debtor_agent=debtor_agent,
            creditor_agent=creditor_agent,
            amount=amount,
            currency=currency,
            priority=priority,
            settlement_timestamp=cre_dt_tm or datetime.now(timezone.utc).isoformat(),
            msg_type=expected_type,
            settlement_method=sttlm_mtd,
            settlement_system=clr_sys,
            debtor_name=debtor_name,
            creditor_name=creditor_name,
        )
        validate_iso20022_payment(payment)
        return payment

    if root_name == "BkToCstmrDbtCdtNtfctn":
        expected_type = "camt.054.001.08"
        if ns and ns != NAMESPACES[expected_type]:
            raise ISO20022ValidationError(
                f"Namespace mismatch for {root_name}: got '{ns}', expected '{NAMESPACES[expected_type]}'."
            )

        msg_id = _find_first_text(msg_root, ["GrpHdr", "MsgId"])
        cre_dt_tm = _find_first_text(msg_root, ["GrpHdr", "CreDtTm"])
        ntry = _find_first_elem(msg_root, ["Ntfctn", "Ntry"])
        if ntry is None:
            raise ISO20022ValidationError("Missing Ntfctn/Ntry in camt.054.001.08.")

        amt_elem = _find_first_elem(ntry, ["Amt"])
        if amt_elem is None:
            raise ISO20022ValidationError("Missing Ntry/Amt in camt.054.001.08.")
        amount, currency = _parse_amount(amt_elem)
        cdt_dbt = _find_first_text(ntry, ["CdtDbtInd"]) or "CRDT"
        status = (
            _find_first_text(ntry, ["Sts", "Cd"])
            or _find_first_text(ntry, ["Sts"])
            or "BOOK"
        )
        bookg_dt = _find_first_text(ntry, ["BookgDt", "DtTm"]) or cre_dt_tm

        tx_dtls = _find_first_elem(ntry, ["NtryDtls", "TxDtls"])
        if tx_dtls is None:
            raise ISO20022ValidationError("Missing NtryDtls/TxDtls in camt.054.001.08.")

        end_to_end_id = _find_first_text(tx_dtls, ["Refs", "EndToEndId"])
        priority = "NORM"  # camt.054 carries no payment priority
        dbtr_agt_el = _find_first_elem(tx_dtls, ["RltdAgts", "DbtrAgt"])
        cdtr_agt_el = _find_first_elem(tx_dtls, ["RltdAgts", "CdtrAgt"])

        debtor_agent = _extract_agent_code(dbtr_agt_el)
        creditor_agent = _extract_agent_code(cdtr_agt_el)

        if not msg_id:
            raise ISO20022ValidationError("Missing GrpHdr/MsgId in camt.054.001.08.")
        if not end_to_end_id:
            raise ISO20022ValidationError("Missing Refs/EndToEndId in camt.054.001.08.")
        if not debtor_agent or not creditor_agent:
            raise ISO20022ValidationError("Missing RltdAgts DbtrAgt/CdtrAgt in camt.054.001.08.")

        payment = ISO20022Payment(
            msg_id=msg_id,
            end_to_end_id=end_to_end_id,
            debtor_agent=debtor_agent,
            creditor_agent=creditor_agent,
            amount=amount,
            currency=currency,
            priority=priority,
            settlement_timestamp=bookg_dt or datetime.now(timezone.utc).isoformat(),
            msg_type=expected_type,
            credit_debit_indicator=cdt_dbt,
            status=status,
        )
        validate_iso20022_payment(payment)
        return payment

    raise ISO20022ValidationError(
        f"Unsupported ISO 20022 message root '{root_name}'. "
        "Supported roots: FIToFICstmrCdtTrf, FICdtTrf, BkToCstmrDbtCdtNtfctn."
    )
