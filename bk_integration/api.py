# Copyright (c) 2025
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import now_datetime, cint


# -------------------------------
# Helpers
# -------------------------------

def _settings():
    """Return BK Integration Settings (Single)."""
    return frappe.get_single("BK Integration Settings")


def _vendor_response(status: int, message: str, data=None):
    """
    Vendor response format:
    {
      "timestamp": "...",
      "message": "Successful",
      "status": 200,
      "data": {...}
    }
    """
    return {
        "timestamp": now_datetime().strftime("%Y-%m-%d %H:%M:%S"),
        "message": message,
        "status": int(status),
        "data": data or {}
    }


def _get_payload():
    """
    Return request payload as dict.
    Works for:
      - Postman JSON body
      - /api/method with form_dict
    """
    payload = {}

    # JSON body
    try:
        if getattr(frappe, "request", None):
            j = frappe.request.get_json(silent=True)
            if isinstance(j, dict):
                payload.update(j)
    except Exception:
        pass

    # form/querystring
    try:
        fd = frappe.local.form_dict or {}
        if isinstance(fd, dict):
            payload.update(fd)
    except Exception:
        pass

    return payload


def _get_bearer_token(payload=None):
    """
    IMPORTANT:
    Frappe can intercept the standard Authorization header for its own auth,
    and can throw AuthenticationError before our method runs.
    So we support BK tokens via custom headers first, and also allow token in body.

    Supported headers (in priority):
      - X-BK-Authorization: Bearer <token>   (RECOMMENDED)
      - BK-Authorization: Bearer <token>
      - X-Authorization: Bearer <token>
      - Authorization: Bearer <token>        (may be intercepted by Frappe in some cases)

    Supported payload fields (fallback):
      - token
      - access_token
      - bearer_token
      - authorization
      - Authorization
    """
    payload = payload or {}

    auth = (
        (frappe.get_request_header("X-BK-Authorization") or "").strip()
        or (frappe.get_request_header("BK-Authorization") or "").strip()
        or (frappe.get_request_header("X-Authorization") or "").strip()
        or (frappe.get_request_header("Authorization") or "").strip()
    )

    # If nothing in headers, look in payload
    if not auth:
        auth = (
            (payload.get("token") or "").strip()
            or (payload.get("access_token") or "").strip()
            or (payload.get("bearer_token") or "").strip()
            or (payload.get("authorization") or "").strip()
            or (payload.get("Authorization") or "").strip()
        )

    if not auth:
        return None

    # if "Bearer xxx"
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()

    # allow raw token
    return auth.strip()


def _require_token():
    payload = _get_payload()
    token = _get_bearer_token(payload)
    if not token:
        frappe.throw(_("Missing Authorization Bearer token"), frappe.AuthenticationError)

    cache_key = f"bk_integration:token:{token}"
    if not frappe.cache().get_value(cache_key):
        frappe.throw(_("Invalid or expired token"), frappe.AuthenticationError)

    return token


def _issue_token(ttl_seconds: int = 86400):
    token = frappe.generate_hash(length=48)
    cache_key = f"bk_integration:token:{token}"
    frappe.cache().set_value(cache_key, 1, expires_in_sec=ttl_seconds)
    return token


def _customer_allowed(customer_group: str) -> bool:
    s = _settings()
    raw = (getattr(s, "allowed_customer_groups", None) or "").strip()

    # comma-separated list, default Student if empty
    if raw:
        allowed = [x.strip() for x in raw.split(",") if x.strip()]
    else:
        allowed = ["Student"]

    return (customer_group or "").strip() in allowed


def _customer_field_exists(fieldname: str) -> bool:
    fieldname = (fieldname or "").strip()
    if not fieldname:
        return False
    if fieldname == "name":
        return True
    meta = frappe.get_meta("Customer")
    return meta.has_field(fieldname)


def _get_customer_by_payer_code(payer_code: str):
    """
    Match payer_code to Customer using settings.payer_code_field.
    Supports:
      - name (Customer ID)
      - any valid Customer field, including custom fields
    """
    s = _settings()
    payer_code = (payer_code or "").strip()
    if not payer_code:
        return None

    field = (getattr(s, "payer_code_field", None) or "name").strip() or "name"

    if not _customer_field_exists(field):
        field = "name"

    if field == "name":
        if frappe.db.exists("Customer", payer_code):
            return frappe.get_doc("Customer", payer_code)
        return None

    name = frappe.db.get_value("Customer", {field: payer_code}, "name")
    return frappe.get_doc("Customer", name) if name else None


def _get_outstanding_invoices(customer: str, company=None):
    filters = {"customer": customer, "docstatus": 1, "outstanding_amount": (">", 0)}
    if company:
        filters["company"] = company

    invs = frappe.get_all(
        "Sales Invoice",
        filters=filters,
        fields=["name", "posting_date", "due_date", "outstanding_amount", "grand_total", "currency", "company"],
        order_by="due_date asc, posting_date asc",
    )

    # attach item names (optional)
    for inv in invs:
        items = frappe.get_all(
            "Sales Invoice Item",
            filters={"parent": inv["name"]},
            fields=["item_name", "description"],
            order_by="idx asc",
        )
        inv["items"] = [
            (i.get("item_name") or (i.get("description") or "")[:60]).strip()
            for i in items
            if (i.get("item_name") or i.get("description"))
        ]

    return invs


def _ensure_txn_log(txn_id: str):
    """
    Create or return BK Payment Transaction record to support idempotency.
    """
    existing = frappe.db.get_value("BK Payment Transaction", {"bk_transaction_id": txn_id}, "name")
    if existing:
        return frappe.get_doc("BK Payment Transaction", existing)

    d = frappe.new_doc("BK Payment Transaction")
    d.bk_transaction_id = txn_id
    d.status = "Received"
    d.received_on = now_datetime()
    d.insert(ignore_permissions=True)
    return d


def _make_payment_for_invoice(invoice_name: str, amount: float, reference_no: str, reference_date=None, mode_of_payment=None):
    """
    Create + submit a Payment Entry against a Sales Invoice (Receive).
    """
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    pe = get_payment_entry("Sales Invoice", invoice_name)

    amt = float(amount or 0)
    pe.paid_amount = amt
    pe.received_amount = amt

    if mode_of_payment:
        pe.mode_of_payment = mode_of_payment

    if pe.references:
        pe.references[0].allocated_amount = amt

    pe.reference_no = reference_no
    if reference_date:
        pe.reference_date = reference_date

    pe.insert(ignore_permissions=True)
    pe.submit()
    return pe.name


# -------------------------------
# Public API (Whitelisted)
# -------------------------------

@frappe.whitelist(allow_guest=True)
def ping():
    # No auth
    return _vendor_response(200, "Successful", {"service": "BK Integration is alive"})


@frappe.whitelist(allow_guest=True)
def authenticate():
    """
    Issues Bearer token for calling protected endpoints.
    Expects JSON:
      {"user_name": "...", "password": "..."}
    Returns vendor format:
      {"status":200,"data":{"token":"Bearer <token>"}}
    """
    s = _settings()
    payload = _get_payload()

    user_name = (payload.get("user_name") or payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()

    if not user_name or not password:
        return _vendor_response(400, "Missing credentials", {})

    cfg_user = (getattr(s, "auth_username", None) or "").strip()
    cfg_pass = (s.get_password("auth_password") or "").strip()

    if user_name != cfg_user or password != cfg_pass:
        return _vendor_response(401, "Unauthorized", {})

    ttl = cint(getattr(s, "token_ttl_seconds", None) or 86400)
    token = _issue_token(ttl_seconds=ttl)

    return _vendor_response(200, "Successful", {"token": f"Bearer {token}", "expires_in": ttl})


@frappe.whitelist(allow_guest=True)
def validate_customer():
    """
    Requires token.
    Expects JSON:
      {"payer_code":"..."} (or payerCode)
    """
    _require_token()
    payload = _get_payload()

    payer_code = (payload.get("payer_code") or payload.get("payerCode") or payload.get("customer_id") or "").strip()
    if not payer_code:
        return _vendor_response(400, "Missing payer_code", {})

    customer = _get_customer_by_payer_code(payer_code)
    if not customer:
        return _vendor_response(404, "Payer not found", {})

    if not _customer_allowed(customer.customer_group):
        return _vendor_response(403, "Payer not allowed", {})

    invs = _get_outstanding_invoices(customer.name)

    services = []
    total_due = 0.0
    for inv in invs:
        amt = float(inv.get("outstanding_amount") or 0)
        total_due += amt
        services.append({
            "service_code": inv["name"],
            "service_name": f"Invoice {inv['name']}",
            "amount": amt,
            "currency": inv.get("currency"),
            "due_date": str(inv.get("due_date") or ""),
            "items": inv.get("items") or [],
        })

    data = {
        "payer_code": payer_code,
        "payer_names": customer.customer_name,
        "customer_group": customer.customer_group,
        "total_due": total_due,
        "services": services,
    }

    return _vendor_response(200, "Successful", data)


@frappe.whitelist(allow_guest=True)
def payment_notification():
    """
    Requires token.
    Stores transaction payload for audit/idempotency.
    """
    _require_token()
    payload = _get_payload()

    txn_id = (payload.get("transaction_id") or payload.get("transactionId") or payload.get("payment_reference") or "").strip()
    if not txn_id:
        return _vendor_response(400, "Missing transaction_id", {})

    tx = _ensure_txn_log(txn_id)
    tx.status = "Notified"
    tx.payer_code = (payload.get("payer_code") or payload.get("payerCode") or "").strip()
    tx.amount = float(payload.get("amount") or 0)
    tx.raw_payload = frappe.as_json(payload)
    tx.save(ignore_permissions=True)

    return _vendor_response(200, "Successful", {"transaction_id": txn_id})


@frappe.whitelist(allow_guest=True)
def payment_callback():
    """
    Requires token.
    Creates Payment Entry and allocates against the invoice (service_code).
    """
    _require_token()
    s = _settings()
    payload = _get_payload()

    txn_id = (payload.get("transaction_id") or payload.get("transactionId") or payload.get("payment_reference") or "").strip()
    payer_code = (payload.get("payer_code") or payload.get("payerCode") or "").strip()
    service_code = (payload.get("service_code") or payload.get("serviceCode") or payload.get("invoice") or "").strip()

    try:
        amount = float(payload.get("amount") or 0)
    except Exception:
        amount = 0.0

    if not txn_id or not payer_code or not service_code or amount <= 0:
        return _vendor_response(400, "Missing required fields", {})

    tx = _ensure_txn_log(txn_id)

    # idempotency
    if tx.status == "Completed" and tx.payment_entry:
        return _vendor_response(200, "Successful", {"payment_entry": tx.payment_entry, "note": "Already processed"})

    customer = _get_customer_by_payer_code(payer_code)
    if not customer:
        tx.status = "Failed"
        tx.raw_payload = frappe.as_json(payload)
        tx.save(ignore_permissions=True)
        return _vendor_response(404, "Payer not found", {})

    if not frappe.db.exists("Sales Invoice", service_code):
        tx.status = "Failed"
        tx.raw_payload = frappe.as_json(payload)
        tx.save(ignore_permissions=True)
        return _vendor_response(404, "Invoice not found", {})

    mode_of_payment = (getattr(s, "default_mode_of_payment", None) or "").strip() or None

    pe_name = _make_payment_for_invoice(
        invoice_name=service_code,
        amount=amount,
        reference_no=txn_id,
        reference_date=now_datetime().date(),
        mode_of_payment=mode_of_payment,
    )

    tx.status = "Completed"
    tx.customer = customer.name
    tx.sales_invoice = service_code
    tx.amount = amount
    tx.payment_entry = pe_name
    tx.raw_payload = frappe.as_json(payload)
    tx.completed_on = now_datetime()
    tx.save(ignore_permissions=True)

    return _vendor_response(200, "Successful", {"payment_entry": pe_name})


@frappe.whitelist(allow_guest=True)
def payment_reversal():
    """
    Requires token.
    Cancels previously created Payment Entry and marks transaction as Reversed.
    """
    _require_token()
    payload = _get_payload()

    txn_id = (payload.get("transaction_id") or payload.get("transactionId") or payload.get("payment_reference") or "").strip()
    if not txn_id:
        return _vendor_response(400, "Missing transaction_id", {})

    tx_name = frappe.db.get_value("BK Payment Transaction", {"bk_transaction_id": txn_id}, "name")
    if not tx_name:
        return _vendor_response(404, "Transaction not found", {})

    tx = frappe.get_doc("BK Payment Transaction", tx_name)

    if tx.status == "Reversed":
        return _vendor_response(200, "Successful", {"note": "Already reversed", "transaction_id": txn_id})

    # cancel payment entry if exists
    if tx.payment_entry and frappe.db.exists("Payment Entry", tx.payment_entry):
        pe = frappe.get_doc("Payment Entry", tx.payment_entry)
        if pe.docstatus == 1:
            pe.cancel()
            pe.save(ignore_permissions=True)

    tx.status = "Reversed"
    tx.reversed_on = now_datetime()
    tx.reversal_payload = frappe.as_json(payload)
    tx.save(ignore_permissions=True)

    return _vendor_response(200, "Successful", {"transaction_id": txn_id, "status": "Reversed"})
