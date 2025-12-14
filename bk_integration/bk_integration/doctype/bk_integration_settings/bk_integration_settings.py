import frappe
from frappe.model.document import Document
from frappe.utils import get_url


class BKIntegrationSettings(Document):
    def validate(self):
        # Auto-detect ERP base URL if empty
        if not (self.erp_base_url or "").strip():
            self.erp_base_url = get_url().rstrip("/")

        self._populate_webhook_urls()
        self._populate_production_ip_best_effort()

    def _populate_webhook_urls(self):
        base = (self.erp_base_url or get_url()).rstrip("/")

        # These are your endpoints (based on the api.py you already have)
        self.auth_url = f"{base}/api/method/bk_integration.api.authenticate"
        self.validation_url = f"{base}/api/method/bk_integration.api.validate_customer"
        self.payment_notification_url = f"{base}/api/method/bk_integration.api.payment_notification"
        self.payment_callback_url = f"{base}/api/method/bk_integration.api.payment_callback"
        self.payment_reversal_url = f"{base}/api/method/bk_integration.api.payment_reversal"

    def _populate_production_ip_best_effort(self):
        # best-effort: detect server IP; not guaranteed to be the public NAT IP
        try:
            import socket
            hostname = socket.gethostname()
            ip = socket.gethostbyname(hostname)
            if ip and ip != "127.0.0.1":
                self.our_production_ip = ip
        except Exception:
            pass
