import frappe
from bk_integration.api import core_authenticate, vendor_response, vendor_err

def get_context(context):
    if frappe.request.method != "POST":
        return vendor_response(vendor_err("Method Not Allowed", status=405))
    return vendor_response(core_authenticate())
