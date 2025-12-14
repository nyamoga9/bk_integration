import frappe
from frappe.model.document import Document
from frappe.utils import get_url


class BKIntegrationSettings(Document):
    def validate(self):
        # Auto-detect ERP base URL if empty
        if not (self.erp_base_url or "").strip():
            self.erp_base_url = get_url().rstrip("/")

        # DO NOT auto-detect production IP anymore (user wants editable + correct public IP)
        # Keep whatever user typed.

        self._populate_webhook_urls()

    def _populate_webhook_urls(self):
        base = (self.erp_base_url or get_url()).rstrip("/")

        self.auth_url = f"{base}/api/method/bk_integration.api.authenticate"
        self.validation_url = f"{base}/api/method/bk_integration.api.validate_customer"
        self.payment_notification_url = f"{base}/api/method/bk_integration.api.payment_notification"
        self.payment_callback_url = f"{base}/api/method/bk_integration.api.payment_callback"
        self.payment_reversal_url = f"{base}/api/method/bk_integration.api.payment_reversal"
