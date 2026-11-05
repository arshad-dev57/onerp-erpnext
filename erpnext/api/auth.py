# Copyright (c) 2026, Bisonstechs and Contributors
# License: GNU General Public License v3. See license.txt

"""Public authentication helpers for company registration + login options."""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.auth import LoginManager
from frappe.utils import cint, cstr, escape_html, validate_email_address
from frappe.utils.password import update_password

from erpnext.api.saas import create_tenant


# No System Manager — that role bypasses User Permissions and leaks other companies.
# Manager alone is not enough: ERPNext grants Customer to Sales User / Sales Master
# Manager and Item to Item Manager, so the owner needs the User + Master tiers too.
COMPANY_OWNER_ROLES = (
	"Accounts Manager",
	"Accounts User",
	"Sales Manager",
	"Sales User",
	"Sales Master Manager",
	"Purchase Manager",
	"Purchase User",
	"Purchase Master Manager",
	"Stock Manager",
	"Stock User",
	"Item Manager",
	"Manufacturing Manager",
	"Manufacturing User",
	"HR Manager",
	"HR User",
	"Projects Manager",
	"Projects User",
	"Inbox User",
	"Report Manager",
)


def _require_signup_enabled() -> None:
	if cint(frappe.get_website_settings("disable_signup")):
		frappe.throw(_("Sign Up is disabled"), title=_("Not Allowed"))


def _clean(value: str | None, max_len: int = 140) -> str:
	return escape_html((value or "").strip())[:max_len]


def _make_abbr(company_name: str) -> str:
	words = re.findall(r"[A-Za-z0-9]+", company_name or "")
	if not words:
		return "CO"
	if len(words) == 1:
		abbr = words[0][:3].upper()
	else:
		abbr = "".join(w[0] for w in words[:4]).upper()
	base = abbr or "CO"
	candidate = base
	idx = 1
	while frappe.db.exists("Company", {"abbr": candidate}):
		idx += 1
		candidate = f"{base}{idx}"
	return candidate


def _pick_chart_of_accounts(country: str) -> str:
	from erpnext.accounts.doctype.account.chart_of_accounts.chart_of_accounts import (
		get_charts_for_country,
	)

	charts = get_charts_for_country(country, with_standard=True) or []
	if "Standard" in charts:
		return "Standard"
	return charts[0] if charts else "Standard"


def _ensure_base_fixtures(country: str) -> None:
	"""Seed ERPNext's base records the first time a site registers a company.

	Sites built with `bench new-site --install-app erpnext` never run the Setup
	Wizard, so records like Warehouse Type "Transit" don't exist -- and Company
	creation makes a "Goods In Transit" warehouse that links to it, which fails
	with "Could not find Warehouse Type: Transit".
	"""
	if frappe.db.exists("Warehouse Type", "Transit"):
		return

	try:
		from erpnext.setup.setup_wizard.operations.install_fixtures import install

		install(country)
	except Exception:
		frappe.log_error(title="Onerp base data install failed", message=frappe.get_traceback())

	# install() skips individual records that error out, so guarantee the one
	# Company creation cannot proceed without.
	if not frappe.db.exists("Warehouse Type", "Transit"):
		warehouse_type = frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"})
		warehouse_type.flags.ignore_permissions = True
		warehouse_type.insert()


def _ensure_company_user_permission(user: str, company: str) -> None:
	"""Restrict the user to a single Company across company-aware DocTypes."""
	existing = frappe.db.exists(
		"User Permission",
		{"user": user, "allow": "Company", "for_value": company},
	)
	if existing:
		frappe.db.set_value("User Permission", existing, "is_default", 1, update_modified=False)
		return

	perm = frappe.get_doc(
		{
			"doctype": "User Permission",
			"user": user,
			"allow": "Company",
			"for_value": company,
			"is_default": 1,
			"apply_to_all_doctypes": 1,
		}
	)
	perm.flags.ignore_permissions = True
	perm.insert()


@frappe.whitelist(allow_guest=True)
def get_registration_options() -> dict:
	"""Countries + enabled currencies for the signup form."""
	_require_signup_enabled()

	countries = frappe.get_all(
		"Country",
		fields=["name"],
		order_by="name asc",
		limit_page_length=500,
	)
	currencies = frappe.get_all(
		"Currency",
		filters={"enabled": 1},
		fields=["name", "currency_name", "symbol"],
		order_by="name asc",
		limit_page_length=500,
	)
	return {
		"countries": [c.name for c in countries],
		"currencies": currencies,
	}


@frappe.whitelist(allow_guest=True)
def register_company(
	first_name: str,
	last_name: str,
	email: str,
	phone: str | None = None,
	company_name: str | None = None,
	country: str | None = None,
	currency: str | None = None,
	password: str | None = None,
	company_address: str | None = None,
	tax_id: str | None = None,
	website: str | None = None,
	company_phone: str | None = None,
	company_description: str | None = None,
	domain: str | None = None,
) -> dict:
	"""
	Multi-step company registration:
	1) Create System User with password
	2) Create Company (COA + defaults)
	3) Log the user in
	"""
	_require_signup_enabled()

	first_name = _clean(first_name, 60)
	last_name = _clean(last_name, 60)
	email = (email or "").strip().lower()
	phone = _clean(phone, 30)
	company_name = _clean(company_name, 140)
	country = _clean(country, 100)
	currency = _clean(currency, 10).upper()
	password = cstr(password or "")
	company_address = _clean(company_address, 500)
	tax_id = _clean(tax_id, 50)
	website = _clean(website, 140)
	company_phone = _clean(company_phone or phone, 30)
	company_description = _clean(company_description, 1000)
	domain = _clean(domain, 100)

	if not first_name or not last_name:
		frappe.throw(_("First name and last name are required"))
	if not email or not validate_email_address(email):
		frappe.throw(_("Valid email is required"))
	if not company_name:
		frappe.throw(_("Company name is required"))
	if not country or not frappe.db.exists("Country", country):
		frappe.throw(_("Valid country is required"))
	if not currency or not frappe.db.exists("Currency", currency):
		frappe.throw(_("Valid currency is required"))
	if len(password) < 8:
		frappe.throw(_("Password must be at least 8 characters"))

	if frappe.db.exists("User", email):
		frappe.throw(_("A user with this email already exists"))
	if frappe.db.exists("Company", company_name):
		frappe.throw(_("A company with this name already exists"))

	max_signups = cint(frappe.get_system_settings("max_signups_allowed_per_hour") or 300)
	if frappe.db.get_creation_count("User", 60) >= max_signups:
		frappe.throw(_("Too many registrations recently. Please try again later."))

	abbr = _make_abbr(company_name)
	chart = _pick_chart_of_accounts(country)

	frappe.flags.in_install = False
	frappe.set_user("Administrator")

	try:
		_ensure_base_fixtures(country)

		frappe.db.set_value("Currency", currency, "enabled", 1, update_modified=False)

		roles = [{"role": role} for role in COMPANY_OWNER_ROLES if frappe.db.exists("Role", role)]
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first_name,
				"last_name": last_name,
				"phone": phone or None,
				"mobile_no": phone or None,
				"send_welcome_email": 0,
				"user_type": "System User",
				"enabled": 1,
				"roles": roles,
			}
		)
		user.flags.ignore_permissions = True
		user.flags.ignore_password_policy = True
		user.insert()
		frappe.clear_messages()

		update_password(user=user.name, pwd=password)

		description_parts = [p for p in (company_description, company_address) if p]
		company = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": company_name,
				"abbr": abbr,
				"default_currency": currency,
				"country": country,
				"phone_no": company_phone or None,
				"email": email,
				"website": website or None,
				"tax_id": tax_id or None,
				"date_of_establishment": frappe.utils.today(),
				"company_description": "\n\n".join(description_parts) or None,
				"domain": domain or None,
				"enable_perpetual_inventory": 1,
				"create_chart_of_accounts_based_on": "Standard Template",
				"chart_of_accounts": chart,
			}
		)
		company.flags.ignore_permissions = True
		company.insert()

		if company_address:
			try:
				address = frappe.get_doc(
					{
						"doctype": "Address",
						"address_title": company_name,
						"address_type": "Office",
						"address_line1": company_address[:140],
						"city": country,
						"country": country,
						"is_your_company_address": 1,
						"links": [{"link_doctype": "Company", "link_name": company.name}],
					}
				)
				address.flags.ignore_permissions = True
				address.flags.ignore_mandatory = True
				address.insert()
			except Exception:
				frappe.log_error(title="Company address creation skipped", message=frappe.get_traceback())

		# Prefer this company for the new user
		for key, value in (
			("company", company.name),
			("currency", currency),
		):
			frappe.defaults.set_user_default(key, value, user.name)

		# Strict company isolation (single-site SaaS)
		_ensure_company_user_permission(user.name, company.name)

		create_tenant(
			owner_email=email,
			company_name=company.name,
			country=country,
			currency=currency,
			phone=phone,
		)

		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Company registration failed", message=frappe.get_traceback())
		raise
	finally:
		frappe.set_user("Guest")

	# Log the user in with the password they just set
	frappe.local.login_manager = LoginManager()
	frappe.local.login_manager.authenticate(user=email, pwd=password)
	frappe.local.login_manager.post_login()

	return {
		"message": "Registered",
		"user": email,
		"company": company_name,
		"home_page": "/app",
	}
