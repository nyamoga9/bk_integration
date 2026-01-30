import frappe
from bk_integration.api import core_ping, vendor_response, vendor_err

def get_context(context):
    if frappe.request.method not in ("GET", "POST"):
        return vendor_response(vendor_err("Method Not Allowed", status=405))
    return vendor_response(core_ping())
