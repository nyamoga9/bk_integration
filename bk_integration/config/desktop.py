from frappe import _

def get_data():
    return [
        {
            "label": _("BK Integration"),
            "icon": "octicon octicon-credit-card",
            "items": [
                {
                    "type": "doctype",
                    "name": "BK Integration Settings",
                    "label": _("BK Integration Settings"),
                    "description": _("Configure BK API integration."),
                },
                {
                    "type": "doctype",
                    "name": "BK Payment Transaction",
                    "label": _("BK Payment Transactions"),
                    "description": _("BK payment callbacks / reversals log."),
                }
            ],
        }
    ]
