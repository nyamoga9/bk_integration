frappe.ui.form.on("BK Integration Settings", {
  refresh(frm) {
    // Show computed URLs immediately
    frm.trigger("generate_urls");
  },

  erp_base_url(frm) {
    frm.trigger("generate_urls");
  },

  enable_integration(frm) {
    frm.trigger("generate_urls");
  },

  generate_urls(frm) {
    const enabled = cint(frm.doc.enable_integration || 0);
    if (!enabled) return;

    const base =
      (frm.doc.erp_base_url || window.location.origin || "").replace(/\/+$/, "");

    frm.set_value("auth_url", `${base}/api/method/bk_integration.api.authenticate`);
    frm.set_value(
      "validation_url",
      `${base}/api/method/bk_integration.api.validate_customer`
    );
    frm.set_value(
      "payment_notification_url",
      `${base}/api/method/bk_integration.api.payment_notification`
    );
    frm.set_value(
      "payment_callback_url",
      `${base}/api/method/bk_integration.api.payment_callback`
    );
    frm.set_value(
      "payment_reversal_url",
      `${base}/api/method/bk_integration.api.payment_reversal`
    );
  },
});
