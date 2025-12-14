frappe.ui.form.on("BK Integration Settings", {
  refresh(frm) {
    frm.trigger("generate_urls");
    frm.trigger("render_test_status");

    if (frm.doc.enable_integration) {
      frm.add_custom_button("Test Connection", () => {
        frm.trigger("test_connection");
      });
    }
  },

  erp_base_url(frm) {
    frm.trigger("generate_urls");
  },

  enable_integration(frm) {
    frm.trigger("generate_urls");
    frm.trigger("render_test_status");
  },

  generate_urls(frm) {
    if (!frm.doc.enable_integration) return;

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

  test_connection(frm) {
    frappe.call({
      method: "bk_integration.api.test_bk_connection",
      freeze: true,
      freeze_message: "Testing BK connection...",
      callback: function (r) {
        frm.reload_doc().then(() => frm.trigger("render_test_status"));
      },
    });
  },

  render_test_status(frm) {
    const s = (frm.doc.last_test_status || "").toUpperCase();
    const msg = frm.doc.last_test_message || "";
    const on = frm.doc.last_test_on || "";

    if (!s) return;

    if (s === "SUCCESS") {
      frappe.show_alert({ message: `BK Connection: SUCCESS`, indicator: "green" });
    } else if (s === "FAILED") {
      frappe.show_alert({ message: `BK Connection: FAILED`, indicator: "red" });
    }

    // Also show nicely in the form intro
    const label =
      s === "SUCCESS"
        ? `<span style="color:#178a2f;font-weight:600;">SUCCESS</span>`
        : `<span style="color:#c0392b;font-weight:600;">FAILED</span>`;

    frm.set_intro(
      `<div>BK Connection Test: ${label}<br>${frappe.utils.escape_html(
        msg
      )}<br><small>Last tested: ${frappe.utils.escape_html(on)}</small></div>`,
      s === "SUCCESS" ? "green" : "red"
    );
  },
});
