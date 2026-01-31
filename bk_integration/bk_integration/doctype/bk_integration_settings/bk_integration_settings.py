# Copyright (c) 2025
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import get_url

class BKIntegrationSettings(Document):
    def validate(self):
        base = (self.erp_base_url or get_url()).rstrip("/")

        # Always generate method URLs (DO NOT change structure)
        self.auth_url = f"{base}/api/method/bk_integration.api.authenticate"
        self.validation_url = f"{base}/api/method/bk_integration.api.validate_customer"
        self.payment_notification_url = f"{base}/api/method/bk_integration.api.payment_notification"
        self.payment_callback_url = f"{base}/api/method/bk_integration.api.payment_callback"
        self.payment_reversal_url = f"{base}/api/method/bk_integration.api.payment_reversal"
