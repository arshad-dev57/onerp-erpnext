# Copyright (c) 2026, Bisonstechs and Contributors
# License: GNU General Public License v3. See license.txt

"""Public authentication helpers for company registration + login options."""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.auth import LoginManager
from frappe.utils import cint, cstr, strip_html_tags, validate_email_address, validate_url
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
	# Tags are dropped, not escaped: Frappe escapes on render, and storing the
	# escaped text turns "Smith & Sons" into "Smith &amp; Sons" in the database.
	return strip_html_tags((value or "").strip()).strip()[:max_len]


def _normalize_phone(value: str) -> str:
	"""Store numbers in the international form the rest of the stack expects."""
	digits = re.sub(r"[\s\-().+]", "", value or "")
	if digits.startswith("00"):
		digits = digits[2:]
	elif digits.startswith("0"):
		digits = "92" + digits[1:]

	if not re.fullmatch(r"\d{9,15}", digits):
		frappe.throw(
			_("Enter a valid phone number, e.g. 03112345678 or +92 311 2345678."),
			title=_("Invalid Phone Number"),
		)
	return digits


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


# The Setup Wizard maps these countries to a non-January financial year; every other
# country uses Jan 1 - Dec 31. Mirrors erpnext.setup.fiscal_years in setup_wizard.js.
FISCAL_YEAR_START = {
	"Afghanistan": "12-21",
	"Australia": "07-01",
	"Bangladesh": "07-01",
	"Costa Rica": "10-01",
	"Egypt": "07-01",
	"Ethiopia": "07-08",
	"Hong Kong": "04-01",
	"India": "04-01",
	"Iran": "06-23",
	"Kenya": "07-01",
	"Malaysia": "07-01",
	"Myanmar": "04-01",
	"Nepal": "07-16",
	"New Zealand": "04-01",
	"Pakistan": "07-01",
	"Singapore": "04-01",
	"South Africa": "03-01",
	"Thailand": "10-01",
	"United Kingdom": "04-01",
}


def _fiscal_year_window(country: str | None) -> tuple[str, str]:
	"""The start/end dates of the financial year containing today for this country."""
	from frappe.utils import add_days, add_years, getdate

	start_month_day = FISCAL_YEAR_START.get(country or "", "01-01")
	today = getdate(frappe.utils.today())
	start = getdate(f"{today.year}-{start_month_day}")
	if start > today:
		start = add_years(start, -1)

	# End date is derived rather than mapped so Fiscal Year.validate_dates always agrees.
	return cstr(start), cstr(add_days(add_years(start, 1), -1))


def _ensure_fiscal_year(company: str, country: str | None) -> None:
	"""Give a company a Fiscal Year covering today.

	The Accounting workspace renders a "Profit and Loss" chart whose report filters come
	from erpnext.utils.get_fiscal_year(); with no Fiscal Year on the site that throws
	"Start Year and End Year are mandatory" as a modal, right after signup, so a new
	tenant reads it as a failed registration.
	"""
	from erpnext.accounts.utils import get_fiscal_years
	from erpnext.setup.setup_wizard.operations.company_setup import get_fy_details

	if get_fiscal_years(frappe.utils.today(), company=company, boolean=True):
		return

	start_date, end_date = _fiscal_year_window(country)
	existing = frappe.db.get_value("Fiscal Year", {"year_start_date": start_date}, "name")

	if existing:
		# Same window, only linked to other tenants: join this company to it.
		fy = frappe.get_doc("Fiscal Year", existing)
		if not any(row.company == company for row in fy.get("companies", [])):
			fy.append("companies", {"company": company})
			fy.flags.ignore_permissions = True
			fy.save()
		return

	fy = frappe.get_doc(
		{
			"doctype": "Fiscal Year",
			"year": get_fy_details(start_date, end_date),
			"year_start_date": start_date,
			"year_end_date": end_date,
			"companies": [{"company": company}],
		}
	)
	fy.flags.ignore_permissions = True
	fy.insert()


def ensure_fiscal_years() -> None:
	"""Backfill Fiscal Years for every company on the site; safe to re-run."""
	for row in frappe.get_all("Company", fields=["name", "country"]):
		try:
			_ensure_fiscal_year(row.name, row.country)
		except Exception:
			# A missing financial year breaks a chart, not the tenant's data.
			frappe.log_error(title=f"Onerp fiscal year setup failed for {row.name}", message=frappe.get_traceback())


def _ensure_setup_complete() -> None:
	"""Lift the desk's forced Setup Wizard redirect once the site has real data.

	Frappe reroutes every /app visit to the Setup Wizard while Installed
	Application.is_setup_complete is 0, and only recomputes that during install or
	migrate -- so a wizard-less site sends a new owner to a page their roles cannot
	open ("Not permitted"). Mark it the way Frappe would, from the same conditions.
	"""
	if frappe.is_setup_complete():
		return

	from frappe.core.doctype.installed_applications.installed_applications import (
		has_company,
		has_non_admin_user,
	)
	from frappe.desk.page.setup_wizard.setup_wizard import enable_setup_wizard_complete

	if not (has_company() and has_non_admin_user()):
		return

	for app_name in ("frappe", "erpnext"):
		enable_setup_wizard_complete(app_name)

	frappe.db.set_single_value("System Settings", "setup_complete", 1, update_modified=False)
	frappe.clear_cache()


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
	fields = ["name", "currency_name", "symbol"]
	currencies = frappe.get_all(
		"Currency",
		filters={"enabled": 1},
		fields=fields,
		order_by="name asc",
		limit_page_length=500,
	)

	# Registration enables whatever currency is picked, so the ones the form defaults to
	# must be selectable on a fresh site -- otherwise the first signup has no local option.
	offered = {c.name for c in currencies}
	for extra in frappe.get_all(
		"Currency",
		filters={"name": ("in", ["PKR", "USD"])},
		fields=fields,
		order_by="name asc",
	):
		if extra.name not in offered:
			currencies.append(extra)

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
	if phone:
		phone = _normalize_phone(_clean(phone, 30))
	company_phone = company_phone or phone
	if company_phone:
		company_phone = _normalize_phone(_clean(company_phone, 30))
	company_name = _clean(company_name, 140)
	country = _clean(country, 100)
	currency = _clean(currency, 10).upper()
	password = cstr(password or "")
	company_address = _clean(company_address, 500)
	tax_id = _clean(tax_id, 50)
	website = _clean(website, 140)
	company_description = _clean(company_description, 1000)
	domain = _clean(domain, 100)

	if website:
		if " " in website:
			frappe.throw(_("Website must be a URL without spaces, e.g. https://example.com"))
		if not website.startswith(("http://", "https://")):
			website = "https://" + website
		if not validate_url(website):
			frappe.throw(_("Invalid website URL"))

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

	# ERPNext names the root Cost Center "<company> - <abbr>", so a company name that
	# fills tabCompany.name would overflow the child's 140-char name and 500 the signup.
	if len(company_name) + len(abbr) + 3 > 140:
		frappe.throw(
			_("Company name is too long. Please use at most {0} characters.").format(
				140 - len(abbr) - 3
			)
		)

	frappe.flags.in_install = False
	frappe.set_user("Administrator")

	try:
		_ensure_base_fixtures(country)

		frappe.db.set_value("Currency", currency, "enabled", 1, update_modified=False)

		roles = [{"role": role} for role in COMPANY_OWNER_ROLES if frappe.db.exists("Role", role)]
		# mobile_no is unique in tabUser; a shared number is not a signup error because
		# login is by email, so keep it in phone only and skip the duplicate mobile_no.
		mobile_no = phone if phone and not frappe.db.exists("User", {"mobile_no": phone}) else None
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first_name,
				"last_name": last_name,
				"phone": phone or None,
				"mobile_no": mobile_no,
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

		# Also backfills tenants that registered before this heal existed.
		ensure_fiscal_years()

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

		_ensure_setup_complete()

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
