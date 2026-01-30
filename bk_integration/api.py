# Copyright (c) 2025
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import now_datetime, cint


# --------------------------------------------------
# Helpers
# --------------------------------------------------

def _settings():
    """Return BK Integration Settings (Single)."""
    return frappe.get_single("BK Integration Settings")


def _get_payload():
    """
    Safely extract payload from:
    - JSON body (Postman / BK)
    - form_dict (/api/method calls)
    """
    payload = {}

    try:
        if frappe.request:
            data = frappe.request.get_json(silent=True)
            if isinstance(data, dict):
                payload.update(data)
    except Exception:
        pass

    try:
        if isinstance(frappe.local.form_dict, dict):
            payload.update(frappe.local.form_dict)
    except Exception:
        pass

    return payload


def _get_bearer_token():
    auth = (frappe.get_request_header("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return None


def _require_token():
    token = _get_bearer_token()
    if not token:
        frappe.throw(_("Missing Authorization Bearer token"), frappe.AuthenticationError)

    cache_key = f"bk_integration:token:{token}"
    if not frappe.cache().get_value(cache_key):
        frappe.throw(_("Invalid or expired token"), frappe.AuthenticationError)

    return token


def _issue_token(ttl_seconds=1800):
    token = frappe.generate_hash(length=48)
    cache_key = f"bk_integration:token:{token}"
    frappe.cache().set_value(cache_key, 1, expires_in_sec=ttl_seconds)
    return token


def _customer_allowed(customer_group):
    s = _settings()
    raw = (s.allowed_customer_groups or "").strip()

    if raw:
        allowed = [x.strip() for x in raw.split(",") if x.strip()]
    else:
        allowed = ["Student"]

    return (customer_group or "").strip() in allowed


def _get_customer_by_payer_code(payer_code):
    s = _settings()
    payer_code = (payer_code or "").strip()
    if not payer_code:
        return None

    field = (s.payer_code_field or "name").strip()

    if field == "name":
        if frappe.db.exists("Customer", payer_code):
            return frappe.get_doc("Customer", payer_code)
        return None

    if not frappe.get_meta("Customer").has_field(field):
        return None

    name = frappe.db.get_value("Customer", {field: payer_code}, "name")
    return frappe.get_doc("Customer", name) if name else None


def _get_outstanding_invoices(customer):
    invs = frappe.get_all(
        "Sales Invoice",
        filters={
            "customer": customer,
            "docstatus": 1,
            "outstanding_amount": (">", 0)
        },
        fields=[
            "name",
            "posting_date",
            "due_date",
            "outstanding_amount",
            "currency"
        ],
        order_by="due_date asc"
    )

    for inv in invs:
        items = frappe.get_all(
            "Sales Invoice Item",
            filters={"parent": inv["name"]},
            fields=["item_name", "description"],
            order_by="idx asc"
        )
        inv["items"] = [
            i.get("item_name") or (i.get("description") or "")[:60]
            for i in items
        ]

    return invs


# --------------------------------------------------
# Public API
# --------------------------------------------------

@frappe.whitelist(allow_guest=True)
def ping():
    return {
        "status": "00",
        "message": "BK Integration is alive"
    }


@frappe.whitelist(allow_guest=True)
def authenticate():
    """
    BK expects TOP-LEVEL JSON response (no "message" wrapper).
    """
    s = _settings()
    payload = _get_payload()
    ts = str(now_datetime())

    user_name = (payload.get("user_name") or payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()

    if not user_name or not password:
        frappe.local.response.http_status_code = 400
        frappe.local.response.update({
            "timestamp": ts,
            "message": "Missing credentials",
            "status": 400,
            "data": {}
        })
        return

    if user_name != (s.auth_username or "").strip() or password != (s.get_password("auth_password") or "").strip():
        frappe.local.response.http_status_code = 401
        frappe.local.response.update({
            "timestamp": ts,
            "message": "Invalid credentials",
            "status": 401,
            "data": {}
        })
        return

    ttl = cint(s.token_ttl_seconds or 1800)
    token = _issue_token(ttl)

    frappe.local.response.http_status_code = 200
    frappe.local.response.update({
        "timestamp": ts,
        "message": "Successful",
        "status": 200,
        "data": {
            "token": f"Bearer {token}"
        }
    })


@frappe.whitelist(allow_guest=True)
def validate_customer():
    _require_token()
    payload = _get_payload()

    payer_code = (payload.get("payer_code") or payload.get("payerCode") or "").strip()
    if not payer_code:
        return {"status": "01", "message": "Missing payer_code"}

    customer = _get_customer_by_payer_code(payer_code)
    if not customer:
        return {"status": "01", "message": "Payer not found"}

    if not _customer_allowed(customer.customer_group):
        return {"status": "01", "message": "Payer not allowed"}

    invoices = _get_outstanding_invoices(customer.name)

    services = []
    total_due = 0

    for inv in invoices:
        amt = float(inv["outstanding_amount"])
        total_due += amt
        services.append({
            "service_code": inv["name"],
            "service_name": f"Invoice {inv['name']}",
            "amount": amt,
            "currency": inv["currency"],
            "due_date": str(inv["due_date"]),
            "items": inv["items"]
        })

    return {
        "status": "00",
        "message": "Success",
        "data": {
            "payer_code": payer_code,
            "payer_names": customer.customer_name,
            "customer_group": customer.customer_group,
            "total_due": total_due,
            "services": services
        }
    }
