import frappe
from frappe.utils import today
from frappe import _


def _get_settings():
    """Fetch and validate BK Integration Settings."""
    settings = frappe.get_single("BK Integration Settings")

    if not settings.enable_integration:
        frappe.throw(_("BK Integration is not enabled."))

    if not settings.student_customer_group:
        frappe.throw(_("Please set 'Student Customer Group' in BK Integration Settings."))

    return settings


@frappe.whitelist()
def ping():
    """Simple health-check endpoint."""
    return "bk_integration API is alive"


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

        inv_filters = {
            "customer": cust_name,
            "docstatus": 1,  # submitted
            "outstanding_amount": [">", 0],
        }

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
