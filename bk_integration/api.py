@frappe.whitelist(allow_guest=True)
def authenticate():
    """
    BK Authentication -> issues Bearer token for calling protected endpoints.
    Response format MUST match BK expectation (top-level JSON).
    """
    from frappe.utils import now_datetime

    s = _settings()
    payload = _get_payload()

    user_name = (payload.get("user_name") or payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()

    if not user_name or not password:
        frappe.local.response.http_status_code = 400
        frappe.local.response.update({
            "timestamp": str(now_datetime()),
            "message": "Missing credentials",
            "status": 400,
            "data": {}
        })
        return

    cfg_user = (s.auth_username or "").strip()
    cfg_pass = (s.get_password("auth_password") or "").strip()

    if user_name != cfg_user or password != cfg_pass:
        frappe.local.response.http_status_code = 401
        frappe.local.response.update({
            "timestamp": str(now_datetime()),
            "message": "Invalid credentials",
            "status": 401,
            "data": {}
        })
        return

    ttl = cint(s.token_ttl_seconds or 1800)
    token = _issue_token(ttl_seconds=ttl)

    # ✅ IMPORTANT: write directly to frappe.local.response
    frappe.local.response.http_status_code = 200
    frappe.local.response.update({
        "timestamp": str(now_datetime()),
        "message": "Successful",
        "status": 200,
        "data": {
            "token": f"Bearer {token}",
            "expires_in": ttl,
            "token_type": "Bearer"
        }
    })
