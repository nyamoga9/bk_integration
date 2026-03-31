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


def _vendor_timestamp():
    # Default vendor timestamp format used by most endpoints
    return now_datetime().strftime("%Y-%m-%d %H:%M:%S")


def _customer_validation_timestamp():
    # Customer validation sample requires 12-hour format, e.g. 2025-09-30 6:03:17 PM
    ts = now_datetime().strftime("%Y-%m-%d %I:%M:%S %p")
    return ts.replace(" 0", " ", 1)


def _format_amount_for_vendor(value):
    try:
        n = float(value or 0)
    except Exception:
        return "0"

    if n.is_integer():
        return str(int(n))

    return ("{0:.2f}".format(n)).rstrip("0").rstrip(".")


def _build_validation_comment(invs):
    if not invs:
        return "school fees"

    first = invs[0]
    items = first.get("items") or []
    if items:
        return items[0]

    return "school fees"


def _vendor_ok(data=None, message="Successful", status=200):
    return {
        "timestamp": _vendor_timestamp(),
        "message": message,
        "status": int(status),
        "data": data or {},
    }


def _vendor_fail(message="Unauthorized", status=401, data=None):
    return {
        "timestamp": _vendor_timestamp(),
        "message": message,
        "status": int(status),
        "data": data or {},
    }


def _get_payload():
    """Return request payload as dict (JSON body + form_dict)."""
    payload = {}
    try:
        if getattr(frappe, "request", None):
            j = frappe.request.get_json(silent=True)
            if isinstance(j, dict):
                payload.update(j)
    except Exception:
        pass

    try:
        fd = frappe.local.form_dict or {}
        if isinstance(fd, dict):
            payload.update(fd)
    except Exception:
        pass

    return payload


def _get_bearer_token():
    """
    Token lookup strategy:
    - Prefer custom headers so we can bypass any framework-level auth assumptions
    - Still accept standard Authorization header if present

    Supported headers (first match wins):
      X-BK-Authorization, BK-Authorization, X-Authorization, Authorization

    Expected value:
      "Bearer <token>"  OR  "<token>"
    """
    auth = (
        (frappe.get_request_header("X-BK-Authorization") or "").strip()
        or (frappe.get_request_header("BK-Authorization") or "").strip()
        or (frappe.get_request_header("X-Authorization") or "").strip()
        or (frappe.get_request_header("Authorization") or "").strip()
    )

    if not auth:
        return None

    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()

    # allow sending token directly (no "Bearer ")
    return auth.strip() or None


def _is_valid_token(token: str) -> bool:
    if not token:
        return False
    cache_key = f"bk_integration:token:{token}"
    return bool(frappe.cache().get_value(cache_key))


def _issue_token(ttl_seconds: int = 86400):
    token = frappe.generate_hash(length=48)
    cache_key = f"bk_integration:token:{token}"
    frappe.cache().set_value(cache_key, 1, expires_in_sec=ttl_seconds)
    return token


def _customer_allowed(customer_group: str) -> bool:
    s = _settings()

    raw = (getattr(s, "allowed_customer_groups", None) or "").strip()
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
    """Match payer_code to Customer using settings.payer_code_field."""
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
    """Create or return BK Payment Transaction record to support idempotency."""
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
    """Create + submit a Payment Entry against a Sales Invoice (Receive)."""
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


def _require_vendor_auth():
    """
    Returns (ok, token_or_response).

    We do NOT raise frappe.AuthenticationError because that produces a Frappe-formatted payload
    that UrubutoPay won't understand.
    """
    token = _get_bearer_token()
    if not token or not _is_valid_token(token):
        return False, _vendor_fail(message="Unauthorized", status=401, data={})
    return True, token


def _require_merchant_code(payload: dict):
    s = _settings()
    expected = (getattr(s, "merchant_code", None) or "").strip()
    got = (payload.get("merchant_code") or payload.get("merchantCode") or "").strip()

    if not expected:
        return False, _vendor_fail(message="Merchant code not configured", status=500, data={})

    if not got:
        return False, _vendor_fail(message="merchant_code is required", status=400, data={})

    if got != expected:
        return False, _vendor_fail(message="Invalid merchant_code", status=401, data={})

    return True, got


# -------------------------------
# Public API (Whitelisted)
# -------------------------------

@frappe.whitelist(allow_guest=True)
def ping():
    """Health check endpoint (no auth)."""
    return _vendor_ok({"alive": True}, message="BK Integration is alive", status=200)


@frappe.whitelist(allow_guest=True)
def authenticate():
    """
    UrubutoPay Authentication -> issues Bearer token for calling protected endpoints.

    Expects JSON:
      {"user_name": "...", "password": "..."}

    Returns (per UrubutoPay docs):
      {
        "timestamp": "YYYY-MM-DD HH:MM:SS",
        "message": "Successful",
        "status": 200,
        "data": {"token": "Bearer <token>"}
      }
    """
    s = _settings()
    payload = _get_payload()

    user_name = (payload.get("user_name") or payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()

    if not user_name or not password:
        return _vendor_fail(message="Missing credentials", status=400, data={})

    cfg_user = (getattr(s, "auth_username", None) or "").strip()
    cfg_pass = (s.get_password("auth_password") or "").strip()

    if user_name != cfg_user or password != cfg_pass:
        return _vendor_fail(message="Unauthorized", status=401, data={})

    ttl = cint(getattr(s, "token_ttl_seconds", None) or 86400)
    token = _issue_token(ttl_seconds=ttl)

    return _vendor_ok({"token": f"Bearer {token}"}, message="Successful", status=200)


@frappe.whitelist(allow_guest=True)
def validate_customer():
    """
    Payer Validation webhook.
    Requires a valid token and merchant_code.

    Expects JSON:
      {"merchant_code": "...", "payer_code": "..."}
    """
    ok, token_or_resp = _require_vendor_auth()
    if not ok:
        return token_or_resp

    payload = _get_payload()

    ok, mc_or_resp = _require_merchant_code(payload)
    if not ok:
        return mc_or_resp

    payer_code = (payload.get("payer_code") or payload.get("payerCode") or payload.get("customer_id") or "").strip()
    if not payer_code:
        return _vendor_fail(message="payer_code is required", status=400, data={})

    customer = _get_customer_by_payer_code(payer_code)
    if not customer:
        return _vendor_fail(message="Payer not found", status=404, data={})

    if not _customer_allowed(customer.customer_group):
        return _vendor_fail(message="Payer not allowed", status=403, data={})

    s = _settings()
    invs = _get_outstanding_invoices(customer.name, company=(getattr(s, "default_company", None) or None))

    bank_name = (getattr(s, "default_service_bank_name", None) or "").strip()
    account_number = (getattr(s, "default_service_account_number", None) or "").strip()

    services = []
    total_due = 0.0
    currency = "RWF"

    for inv in invs:
        amt = float(inv.get("outstanding_amount") or 0)
        total_due += amt
        currency = (inv.get("currency") or currency)

        services.append({
            "service_id": inv["name"],
            "service_code": inv["name"],
            "service_name": f"Invoice {inv['name']}",
            "amount": amt,
            "account_number": account_number,
            "bank_name": bank_name,
            "is_recurring_enabled": False,
        })

    data = {
        "merchant_code": (getattr(s, "merchant_code", None) or "").strip(),
        "payer_code": payer_code,
        "payer_names": customer.customer_name,
        "currency": currency,
        "payer_must_pay_total_amount": "NO",
        "amount": _format_amount_for_vendor(total_due),
        "comment": _build_validation_comment(invs),
    }

    return {
        "status": 200,
        "message": "validated successfully",
        "timestamp": _customer_validation_timestamp(),
        "data": data,
    }


@frappe.whitelist(allow_guest=True)
def payment_notification():
    """Payment Notification webhook (pre-confirmation)."""
    ok, token_or_resp = _require_vendor_auth()
    if not ok:
        return token_or_resp

    payload = _get_payload()

    ok, mc_or_resp = _require_merchant_code(payload)
    if not ok:
        return mc_or_resp

    txn_id = (payload.get("transaction_id") or payload.get("transactionId") or payload.get("payment_reference") or "").strip()
    if not txn_id:
        return _vendor_fail(message="transaction_id is required", status=400, data={})

    tx = _ensure_txn_log(txn_id)
    tx.status = "Notified"
    tx.payer_code = (payload.get("payer_code") or payload.get("payerCode") or "").strip()
    tx.amount = float(payload.get("amount") or 0)
    tx.raw_payload = frappe.as_json(payload)
    tx.save(ignore_permissions=True)

    return _vendor_ok({"transaction_id": txn_id}, message="Received", status=200)


@frappe.whitelist(allow_guest=True)
def payment_callback():
    """Payment Callback webhook (confirmation)."""
    ok, token_or_resp = _require_vendor_auth()
    if not ok:
        return token_or_resp

    s = _settings()
    payload = _get_payload()

    ok, mc_or_resp = _require_merchant_code(payload)
    if not ok:
        return mc_or_resp

    txn_id = (payload.get("transaction_id") or payload.get("transactionId") or payload.get("payment_reference") or "").strip()
    payer_code = (payload.get("payer_code") or payload.get("payerCode") or "").strip()
    service_code = (payload.get("service_code") or payload.get("serviceCode") or payload.get("invoice") or "").strip()

    try:
        amount = float(payload.get("amount") or 0)
    except Exception:
        amount = 0.0

    if not txn_id or not payer_code or not service_code or amount <= 0:
        return _vendor_fail(message="Missing required fields (transaction_id, payer_code, service_code, amount)", status=400, data={})

    tx = _ensure_txn_log(txn_id)

    if tx.status == "Completed" and tx.payment_entry:
        return _vendor_ok({"payment_entry": tx.payment_entry}, message="Already processed", status=200)

    customer = _get_customer_by_payer_code(payer_code)
    if not customer:
        tx.status = "Failed"
        tx.raw_payload = frappe.as_json(payload)
        tx.save(ignore_permissions=True)
        return _vendor_fail(message="Payer not found", status=404, data={})

    if not frappe.db.exists("Sales Invoice", service_code):
        tx.status = "Failed"
        tx.raw_payload = frappe.as_json(payload)
        tx.save(ignore_permissions=True)
        return _vendor_fail(message="Invoice not found (service_code)", status=404, data={})

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

    return _vendor_ok({"payment_entry": pe_name}, message="Successful", status=200)


@frappe.whitelist(allow_guest=True)
def payment_reversal():
    """Payment Reversal webhook."""
    ok, token_or_resp = _require_vendor_auth()
    if not ok:
        return token_or_resp

    payload = _get_payload()

    ok, mc_or_resp = _require_merchant_code(payload)
    if not ok:
        return mc_or_resp

    txn_id = (payload.get("transaction_id") or payload.get("transactionId") or payload.get("payment_reference") or "").strip()
    if not txn_id:
        return _vendor_fail(message="transaction_id is required", status=400, data={})

    tx_name = frappe.db.get_value("BK Payment Transaction", {"bk_transaction_id": txn_id}, "name")
    if not tx_name:
        return _vendor_fail(message="Transaction not found", status=404, data={})

    tx = frappe.get_doc("BK Payment Transaction", tx_name)

    if tx.status == "Reversed":
        return _vendor_ok({"transaction_id": txn_id}, message="Already reversed", status=200)

    if tx.payment_entry and frappe.db.exists("Payment Entry", tx.payment_entry):
        pe = frappe.get_doc("Payment Entry", tx.payment_entry)
        if pe.docstatus == 1:
            pe.cancel()
            pe.save(ignore_permissions=True)

    tx.status = "Reversed"
    tx.reversed_on = now_datetime()
    tx.reversal_payload = frappe.as_json(payload)
    tx.save(ignore_permissions=True)

    return _vendor_ok({"transaction_id": txn_id}, message="Reversed", status=200)


@frappe.whitelist()
def test_bk_connection():
    """
    Basic connectivity test (optional):
    - Checks that BK Base URL is set
    - Tries a GET request to that base URL (and also tries /health and /ping as fallbacks)
    """
    import requests

    s = _settings()
    if not (getattr(s, "bk_base_url", None) or "").strip():
        frappe.throw(_("BK API Base URL is required to test connection."))

    base = s.bk_base_url.rstrip("/")

    candidates = [
        base,
        base + "/health",
        base + "/ping",
        base + "/api/health",
        base + "/api/ping",
    ]

    ok = False
    last_msg = ""
    status_code = None

    for url in candidates:
        try:
            r = requests.get(url, timeout=10)
            status_code = r.status_code
            if 200 <= r.status_code < 300:
                ok = True
                last_msg = f"Success: GET {url} returned {r.status_code}"
                break
            else:
                last_msg = f"Reached {url} but got HTTP {r.status_code}"
        except Exception as e:
            last_msg = f"Failed to reach {url}: {e}"

    return {"ok": ok, "message": last_msg, "http_status": status_code}
