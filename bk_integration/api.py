# Copyright (c) 2025
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import now_datetime, cint


def _settings():
    """Return BK Integration Settings (Single)."""
    return frappe.get_single("BK Integration Settings")


def _get_bearer_token():
    auth = frappe.get_request_header("Authorization") or ""
    auth = auth.strip()
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


def _issue_token(ttl_seconds: int = 86400):
    token = frappe.generate_hash(length=48)
    cache_key = f"bk_integration:token:{token}"
    frappe.cache().set_value(cache_key, 1, expires_in_sec=ttl_seconds)
    return token


def _customer_allowed(customer_group: str) -> bool:
    s = _settings()
    allowed = []
    if getattr(s, "allowed_customer_groups", None):
        # MultiSelect stores \n separated values
        allowed = [x.strip() for x in (s.allowed_customer_groups or "").split("\n") if x.strip()]
    if not allowed:
        # fallback default
        allowed = ["Student"]
    return (customer_group or "") in allowed


def _get_customer_by_payer_code(payer_code: str):
    """Match payer_code to Customer 'name' or to settings.payer_code_field."""
    s = _settings()
    payer_code = (payer_code or "").strip()

    field = (getattr(s, "payer_code_field", None) or "name").strip()
    if field not in ("name", "customer_name", "tax_id"):
        # custom field support (best-effort)
        field = "name"

    if field == "name":
        doc = frappe.get_doc("Customer", payer_code) if frappe.db.exists("Customer", payer_code) else None
        return doc

    # lookup by field
    name = frappe.db.get_value("Customer", {field: payer_code}, "name")
    return frappe.get_doc("Customer", name) if name else None


def _get_outstanding_invoices(customer: str, company: str | None = None):
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
            filters={"parent": inv["name"], "docstatus": 1},
            fields=["item_name", "description"],
            order_by="idx asc",
        )
        inv["items"] = [i["item_name"] or (i["description"] or "")[:60] for i in items if (i.get("item_name") or i.get("description"))]
    return invs


def _ensure_txn_log(txn_id: str):
    if frappe.db.exists("BK Payment Transaction", {"bk_transaction_id": txn_id}):
        return frappe.get_doc("BK Payment Transaction", {"bk_transaction_id": txn_id})

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
    # set paid amount (supports partial)
    amt = float(amount or 0)
    pe.paid_amount = amt
    pe.received_amount = amt

    if mode_of_payment:
        pe.mode_of_payment = mode_of_payment

    # references table already has the invoice row; adjust allocation
    if pe.references:
        pe.references[0].allocated_amount = amt

    pe.reference_no = reference_no
    if reference_date:
        pe.reference_date = reference_date

    pe.insert(ignore_permissions=True)
    pe.submit()
    return pe.name


@frappe.whitelist(allow_guest=True, methods=["GET"])
def ping():
    """Health check endpoint (no auth)."""
    return {"status": "00", "message": "BK Integration is alive"}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def authenticate():
    """
    UrubutoPay / BK Authentication (Bearer token).
    Expects JSON:
      {"user_name": "...", "password": "..."}
    Returns:
      {"status":"00","message":"Success","token":"...","token_type":"Bearer","expires_in":86400}
    """
    s = _settings()
    payload = frappe.local.form_dict or {}
    user_name = (payload.get("user_name") or payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()

    if not user_name or not password:
        return {"status": "01", "message": "Missing credentials"}

    if user_name != (s.auth_username or "").strip() or password != (s.get_password("auth_password") or "").strip():
        return {"status": "01", "message": "Invalid credentials"}

    ttl = cint(getattr(s, "token_ttl_seconds", None) or 86400)
    token = _issue_token(ttl_seconds=ttl)

    return {"status": "00", "message": "Success", "token": token, "token_type": "Bearer", "expires_in": ttl}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def validate_customer():
    """
    Payer Validation webhook.
    Requires Authorization: Bearer <token>
    Expects JSON:
      {"merchant_code":"...", "payer_code":"..."}
    Returns customer details + outstanding invoices as services.
    """
    _require_token()

    payload = frappe.local.form_dict or {}
    payer_code = (payload.get("payer_code") or payload.get("payerCode") or payload.get("customer_id") or "").strip()
    if not payer_code:
        return {"status": "01", "message": "Missing payer_code"}

    customer = _get_customer_by_payer_code(payer_code)
    if not customer:
        return {"status": "01", "message": "Payer not found"}

    if not _customer_allowed(customer.customer_group):
        return {"status": "01", "message": "Payer not allowed"}

    invs = _get_outstanding_invoices(customer.name)

    services = []
    total_due = 0.0
    for inv in invs:
        amt = float(inv.get("outstanding_amount") or 0)
        total_due += amt
        services.append(
            {
                "service_code": inv["name"],  # treat Sales Invoice number as service_code
                "service_name": f"Invoice {inv['name']}",
                "amount": amt,
                "currency": inv.get("currency"),
                "due_date": str(inv.get("due_date") or ""),
                "items": inv.get("items") or [],
            }
        )

    return {
        "status": "00",
        "message": "Success",
        "data": {
            "payer_code": payer_code,
            "payer_names": customer.customer_name,
            "customer_group": customer.customer_group,
            "total_due": total_due,
            "services": services,
        },
    }


@frappe.whitelist(allow_guest=True, methods=["POST"])
def payment_notification():
    """
    Payment Notification webhook (pre-confirmation).
    Stores transaction payload for audit/idempotency.
    Requires Authorization: Bearer <token>
    """
    _require_token()
    payload = frappe.local.form_dict or {}
    txn_id = (payload.get("transaction_id") or payload.get("transactionId") or payload.get("payment_reference") or "").strip()
    if not txn_id:
        return {"status": "01", "message": "Missing transaction_id"}

    tx = _ensure_txn_log(txn_id)
    tx.status = "Notified"
    tx.payer_code = (payload.get("payer_code") or "").strip()
    tx.amount = float(payload.get("amount") or 0)
    tx.raw_payload = frappe.as_json(payload)
    tx.save(ignore_permissions=True)

    return {"status": "00", "message": "Received"}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def payment_callback():
    """
    Payment Callback webhook (confirmation).
    Creates Payment Entry and allocates against the invoice (service_code).
    Requires Authorization: Bearer <token>
    """
    _require_token()
    s = _settings()
    payload = frappe.local.form_dict or {}

    txn_id = (payload.get("transaction_id") or payload.get("transactionId") or payload.get("payment_reference") or "").strip()
    payer_code = (payload.get("payer_code") or payload.get("payerCode") or "").strip()
    service_code = (payload.get("service_code") or payload.get("serviceCode") or payload.get("invoice") or "").strip()
    amount = float(payload.get("amount") or 0)

    if not txn_id or not payer_code or not service_code or amount <= 0:
        return {"status": "01", "message": "Missing required fields (transaction_id, payer_code, service_code, amount)"}

    tx = _ensure_txn_log(txn_id)

    # idempotency: if already completed, return ok
    if tx.status == "Completed" and tx.payment_entry:
        return {"status": "00", "message": "Already processed", "data": {"payment_entry": tx.payment_entry}}

    customer = _get_customer_by_payer_code(payer_code)
    if not customer:
        tx.status = "Failed"
        tx.raw_payload = frappe.as_json(payload)
        tx.save(ignore_permissions=True)
        return {"status": "01", "message": "Payer not found"}

    if not frappe.db.exists("Sales Invoice", service_code):
        tx.status = "Failed"
        tx.raw_payload = frappe.as_json(payload)
        tx.save(ignore_permissions=True)
        return {"status": "01", "message": "Invoice not found (service_code)"}

    # Create payment entry
    mode_of_payment = getattr(s, "default_mode_of_payment", None) or None
    pe_name = _make_payment_for_invoice(service_code, amount, reference_no=txn_id, reference_date=now_datetime().date(), mode_of_payment=mode_of_payment)

    tx.status = "Completed"
    tx.customer = customer.name
    tx.sales_invoice = service_code
    tx.amount = amount
    tx.payment_entry = pe_name
    tx.raw_payload = frappe.as_json(payload)
    tx.completed_on = now_datetime()
    tx.save(ignore_permissions=True)

    return {"status": "00", "message": "Success", "data": {"payment_entry": pe_name}}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def payment_reversal():
    """
    Payment Reversal webhook.
    Cancels previously created Payment Entry (best-practice for reversal),
    and marks transaction as Reversed.
    Requires Authorization: Bearer <token>
    """
    _require_token()
    payload = frappe.local.form_dict or {}
    txn_id = (payload.get("transaction_id") or payload.get("transactionId") or payload.get("payment_reference") or "").strip()
    if not txn_id:
        return {"status": "01", "message": "Missing transaction_id"}

    tx = frappe.get_doc("BK Payment Transaction", {"bk_transaction_id": txn_id}) if frappe.db.exists("BK Payment Transaction", {"bk_transaction_id": txn_id}) else None
    if not tx:
        return {"status": "01", "message": "Transaction not found"}

    if tx.status == "Reversed":
        return {"status": "00", "message": "Already reversed"}

    # cancel payment entry if exists
    if tx.payment_entry and frappe.db.exists("Payment Entry", tx.payment_entry):
        pe = frappe.get_doc("Payment Entry", tx.payment_entry)
        if pe.docstatus == 1:
            pe.cancel()

    tx.status = "Reversed"
    tx.reversed_on = now_datetime()
    tx.reversal_payload = frappe.as_json(payload)
    tx.save(ignore_permissions=True)

    return {"status": "00", "message": "Reversed"}
