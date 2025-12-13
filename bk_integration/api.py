import frappe
from frappe.utils import today, now_datetime
from frappe import _


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_settings():
    """Fetch and validate BK Integration Settings."""
    settings = frappe.get_single("BK Integration Settings")

    if not settings.enable_integration:
        frappe.throw(_("BK Integration is not enabled."))

    if not settings.student_customer_group:
        frappe.throw(_("Please set 'Student Customer Group' in BK Integration Settings."))

    return settings


def _get_student_invoices(customer_name, settings):
    """Internal helper: fetch outstanding invoices for a student customer."""
    inv_filters = {
        "customer": customer_name,
        "docstatus": 1,  # submitted
        "outstanding_amount": [">", 0],
    }

    # Optional company filter
    if getattr(settings, "default_company", None):
        inv_filters["company"] = settings.default_company

    invoices = frappe.get_all(
        "Sales Invoice",
        filters=inv_filters,
        fields=[
            "name",
            "posting_date",
            "due_date",
            "grand_total",
            "outstanding_amount",
            "currency",
        ],
        order_by="due_date asc",
    )

    invoice_data = []

    for inv in invoices:
        invoice_entry = {
            "invoice_no": inv["name"],
            "posting_date": str(inv["posting_date"]),
            "due_date": str(inv["due_date"]),
            "grand_total": float(inv["grand_total"] or 0),
            "outstanding_amount": float(inv["outstanding_amount"] or 0),
            "currency": inv["currency"],
        }

        # Optional: include item details
        if getattr(settings, "expose_item_details", False):
            items = frappe.get_all(
                "Sales Invoice Item",
                filters={"parent": inv["name"]},
                fields=["item_code", "item_name", "amount"],
            )

            invoice_entry["items"] = [
                {
                    "item_code": it["item_code"],
                    "item_name": it["item_name"],
                    "amount": float(it["amount"] or 0),
                }
                for it in items
            ]

        invoice_data.append(invoice_entry)

    return invoice_data


# ---------------------------------------------------------------------------
# Simple health check
# ---------------------------------------------------------------------------

@frappe.whitelist()
def ping():
    """Simple health-check endpoint."""
    return "bk_integration API is alive"


# ---------------------------------------------------------------------------
# Main "pull" endpoint: list students + outstanding invoices
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def get_student_customers_with_invoices(changed_since=None):
    """
    Public API for BK to:
      - get list of customers in the configured Customer Group (students)
      - with their outstanding Sales Invoices and optional item lines.

    URL:
      /api/method/bk_integration.api.get_student_customers_with_invoices
    """

    settings = _get_settings()

    customer_filters = {
        "customer_group": settings.student_customer_group,
        "disabled": 0,
    }

    customers = frappe.get_all(
        "Customer",
        filters=customer_filters,
        fields=["name", "customer_name", "customer_group"],
    )

    result = []

    for cust in customers:
        cust_name = cust["name"]

        invoice_data = _get_student_invoices(cust_name, settings)

        if invoice_data:
            result.append(
                {
                    "customer_id": cust_name,
                    "customer_name": cust["customer_name"],
                    "customer_group": cust["customer_group"],
                    "invoices": invoice_data,
                }
            )

    return {
        "timestamp": today(),
        "customer_count": len(result),
        "customers": result,
    }


# ---------------------------------------------------------------------------
# VALIDATION ENDPOINT
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def validate_customer(customer_id=None):
    """
    Called by BK before accepting payment.

    Query params:
      - customer_id  (required)  -> ERPNext Customer name / ID

    Response (examples):

    Customer not found / not in student group:
    {
      "status": "NOT_FOUND",
      "message": "Customer not found or not a student",
      "customer_id": "XXXX"
    }

    Successful validation:
    {
      "status": "OK",
      "customer_id": "STU-0001",
      "customer_name": "John Doe",
      "customer_group": "Students",
      "total_outstanding": 150000.0,
      "currency": "RWF",
      "invoices": [ ... same structure as get_student_customers_with_invoices ... ]
    }
    """

    if not customer_id:
        frappe.throw(_("Missing parameter: customer_id"), frappe.ValidationError)

    settings = _get_settings()

    # Customer must exist and be in the configured student group
    cust_filters = {
        "name": customer_id,
        "disabled": 0,
        "customer_group": settings.student_customer_group,
    }

    cust = frappe.db.get_value(
        "Customer",
        cust_filters,
        ["name", "customer_name", "customer_group"],
        as_dict=True,
    )

    if not cust:
        return {
            "status": "NOT_FOUND",
            "message": "Customer not found or not in the allowed customer group",
            "customer_id": customer_id,
        }

    invoices = _get_student_invoices(cust["name"], settings)

    total_outstanding = sum(inv["outstanding_amount"] for inv in invoices) if invoices else 0

    if not invoices:
        return {
            "status": "NO_DUES",
            "message": "Customer found, but no outstanding invoices",
            "customer_id": cust["name"],
            "customer_name": cust["customer_name"],
            "customer_group": cust["customer_group"],
            "total_outstanding": 0,
            "currency": getattr(settings, "default_currency", None),
            "invoices": [],
        }

    currency = invoices[0]["currency"] if invoices else getattr(settings, "default_currency", None)

    return {
        "status": "OK",
        "customer_id": cust["name"],
        "customer_name": cust["customer_name"],
        "customer_group": cust["customer_group"],
        "total_outstanding": total_outstanding,
        "currency": currency,
        "invoices": invoices,
    }


# ---------------------------------------------------------------------------
# PAYMENT NOTIFICATION (stub)
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def payment_notification():
    """
    Optional: BK can notify that a payment process has started.

    For now this is a stub that simply echoes the payload and timestamp.
    Later we can log this into Integration Request or a custom doctype.

    Expected JSON (example from BK):
    {
      "customer_id": "STU-0001",
      "invoice_no": "SINV-0005",
      "amount": 50000,
      "transaction_ref": "BK-TXN-123",
      "status": "PENDING"
    }
    """

    data = frappe.request.get_json(silent=True) or {}

    return {
        "status": "RECEIVED",
        "type": "PAYMENT_NOTIFICATION",
        "timestamp": str(now_datetime()),
        "payload": data,
    }


# ---------------------------------------------------------------------------
# PAYMENT CALLBACK (stub – will later create Payment Entry)
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def payment_callback():
    """
    Called by BK when a payment is CONFIRMED.

    For now this is a stub that just echoes back the payload.
    Next step: create a Payment Entry and allocate it to the invoice(s).

    Expected JSON (example – we can refine with BK):
    {
      "customer_id": "STU-0001",
      "invoice_no": "SINV-0005",
      "amount_paid": 50000,
      "currency": "RWF",
      "transaction_ref": "BK-TXN-123",
      "bank_reference": "BK-123456",
      "payment_date": "2025-11-27T10:35:00Z"
    }
    """

    data = frappe.request.get_json(silent=True) or {}

    # TODO (later): validate payload, create Payment Entry, update invoice

    return {
        "status": "ACCEPTED",
        "message": "Payment callback received. Processing logic to be implemented.",
        "timestamp": str(now_datetime()),
        "payload": data,
    }


# ---------------------------------------------------------------------------
# PAYMENT REVERSAL (stub)
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def payment_reversal():
    """
    Called by BK when a previously confirmed payment is reversed / cancelled.

    For now this is a stub that simply echoes the payload.
    Later we will:
      - find the original Payment Entry
      - create a reversing Journal Entry / Payment Entry

    Expected JSON (example):
    {
      "customer_id": "STU-0001",
      "invoice_no": "SINV-0005",
      "amount_reversed": 50000,
      "currency": "RWF",
      "transaction_ref": "BK-TXN-123",
      "reversal_ref": "BK-REV-888",
      "reason": "Customer refund"
    }
    """

    data = frappe.request.get_json(silent=True) or {}

    return {
        "status": "REVERSAL_RECEIVED",
        "message": "Payment reversal received. Processing logic to be implemented.",
        "timestamp": str(now_datetime()),
        "payload": data,
    }