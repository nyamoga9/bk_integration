# Copyright (c) 2025
# For license information, please see license.txt

import socket
from frappe.model.document import Document


class BKIntegrationSettings(Document):
    def validate(self):
        # Auto-fill webhook URLs for the current ERP instance (erp_base_url)
        self.set_webhook_urls()
        # Best-effort production IP (editable by user)
        self.set_production_ip_if_blank()

    def set_webhook_urls(self):
        base = (self.erp_base_url or "").rstrip("/")
        if not base:
            return

        # Use website routes (NOT /api/method) so vendor gets top-level JSON (no "message" wrapper)
        self.auth_url = f"{base}/urubutopay/auth"
        self.validation_url = f"{base}/urubutopay/validate"
        self.payment_notification_url = f"{base}/urubutopay/payment-notification"
        self.payment_callback_url = f"{base}/urubutopay/payment-callback"
        self.payment_reversal_url = f"{base}/urubutopay/payment-reversal"

    def set_production_ip_if_blank(self):
        # Only set if empty so it's user-editable
        if self.our_production_ip:
            return
        try:
            self.our_production_ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            pass
