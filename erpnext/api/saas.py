# Copyright (c) 2026, Bisonstechs and Contributors
# License: GNU General Public License v3. See license.txt

"""Single-site SaaS tenancy: one company per signup, time-boxed trial, admin APIs.

Tenant bookkeeping lives in its own SQLite file (default ~/onerp-data/onerp_saas.db,
override with `onerp_saas_db` in common_site_config.json or ONERP_SAAS_DB) so it never
touches the ERPNext schema. Only users that have a tenant row are gated — Administrator
and the site owner's own account are unaffected.
"""

from __future__ import annotations

import math
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import cint, cstr, format_date, now_datetime

ADMIN_ROLE = "Onerp Admin"
TS_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_DB_PATH = os.path.expanduser("~/onerp-data/onerp_saas.db")
DEFAULT_TRIAL_DAYS = 5

TENANT_STATUSES = ("trial", "active", "expired", "suspended")

# Frappe's own plumbing modules -- writes here must survive an expired trial
INFRA_MODULES = ("Core", "Desk", "Email", "Printing", "Geo", "Utilities", "Automation")


def _conf(key: str, default=None):
	value = os.environ.get(f"ONERP_{key.upper()}")
	if value:
		return value
	return (frappe.conf or {}).get(f"onerp_{key.lower()}", default)


def _db_path() -> str:
	return os.path.expanduser(cstr(_conf("saas_db", DEFAULT_DB_PATH)))


def _default_trial_days() -> int:
	return cint(_conf("trial_days", DEFAULT_TRIAL_DAYS)) or DEFAULT_TRIAL_DAYS


def _ts(value: datetime) -> str:
	return value.strftime(TS_FORMAT)


def _parse(value: str) -> datetime:
	return datetime.strptime(value[:19], TS_FORMAT)


_SCHEMA = (
	"""
	CREATE TABLE IF NOT EXISTS tenants (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		owner_email TEXT NOT NULL UNIQUE,
		company_name TEXT,
		country TEXT,
		currency TEXT,
		phone TEXT,
		status TEXT NOT NULL DEFAULT 'trial',
		trial_start TEXT NOT NULL,
		trial_end TEXT NOT NULL,
		plan TEXT,
		notes TEXT,
		created_at TEXT NOT NULL,
		updated_at TEXT NOT NULL
	)
	""",
	"""
	CREATE TABLE IF NOT EXISTS trial_history (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		tenant_id INTEGER NOT NULL,
		action TEXT NOT NULL,
		from_end TEXT,
		to_end TEXT,
		days INTEGER,
		reason TEXT,
		performed_by TEXT,
		created_at TEXT NOT NULL
	)
	""",
	"CREATE INDEX IF NOT EXISTS idx_tenants_company ON tenants(company_name)",
)


@contextmanager
def _db():
	path = _db_path()
	os.makedirs(os.path.dirname(path), exist_ok=True)
	conn = sqlite3.connect(path)
	conn.row_factory = sqlite3.Row
	conn.execute("PRAGMA journal_mode=WAL")
	conn.execute("PRAGMA busy_timeout=5000")
	try:
		if not getattr(frappe.local, "onerp_saas_ready", False):
			for statement in _SCHEMA:
				conn.execute(statement)
			frappe.local.onerp_saas_ready = True
		yield conn
		conn.commit()
	except Exception:
		conn.rollback()
		raise
	finally:
		conn.close()


def _log_history(conn, tenant_id, action, from_end=None, to_end=None, days=None, reason=None):
	conn.execute(
		"""INSERT INTO trial_history
			(tenant_id, action, from_end, to_end, days, reason, performed_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
		(tenant_id, action, from_end, to_end, days, reason, frappe.session.user, _ts(now_datetime())),
	)


def _with_state(tenant: dict) -> dict:
	"""days_left / blocked are derived, never stored, so an extension takes effect at once."""
	remaining = _parse(tenant["trial_end"]) - now_datetime()
	seconds = remaining.total_seconds()
	tenant["expired"] = seconds <= 0
	tenant["days_left"] = max(0, int(math.ceil(seconds / 86400)))
	tenant["blocked"] = tenant["status"] in ("expired", "suspended") or (
		tenant["status"] != "active" and tenant["expired"]
	)
	return tenant


def _tenant_memo() -> dict:
	# frappe.local is a werkzeug Local without dict methods, so the per-request
	# cache has to be lazily attached as an attribute.
	try:
		return frappe.local.onerp_tenant_memo
	except AttributeError:
		frappe.local.onerp_tenant_memo = {}
		return frappe.local.onerp_tenant_memo


def _reset_tenant_memo() -> None:
	frappe.local.onerp_tenant_memo = {}


def create_tenant(
	owner_email: str,
	company_name: str | None = None,
	country: str | None = None,
	currency: str | None = None,
	phone: str | None = None,
	trial_days: int | None = None,
) -> dict:
	days = cint(trial_days or _default_trial_days())
	start = now_datetime()
	end = start + timedelta(days=days)
	stamp = _ts(start)
	email = cstr(owner_email).strip().lower()

	with _db() as conn:
		conn.execute(
			"""INSERT INTO tenants
				(owner_email, company_name, country, currency, phone, status,
                 trial_start, trial_end, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'trial', ?, ?, ?, ?)
               ON CONFLICT(owner_email) DO UPDATE SET
                 company_name = excluded.company_name,
                 country = excluded.country,
                 currency = excluded.currency,
                 phone = excluded.phone,
                 status = 'trial',
                 trial_start = excluded.trial_start,
                 trial_end = excluded.trial_end,
                 updated_at = excluded.updated_at""",
			(email, company_name, country, currency, phone, _ts(start), _ts(end), stamp, stamp),
		)
		row = conn.execute("SELECT * FROM tenants WHERE owner_email = ?", (email,)).fetchone()
		_log_history(conn, row["id"], "created", to_end=row["trial_end"], days=days)

	_reset_tenant_memo()
	return _with_state(dict(row))


def get_tenant(tenant_id: int | None = None, email: str | None = None, company: str | None = None) -> dict | None:
	clauses, params = [], []
	if tenant_id:
		clauses.append("id = ?")
		params.append(cint(tenant_id))
	if email:
		clauses.append("owner_email = ?")
		params.append(cstr(email).strip().lower())
	if company:
		clauses.append("company_name = ?")
		params.append(company)
	if not clauses:
		return None

	with _db() as conn:
		row = conn.execute(
			f"SELECT * FROM tenants WHERE {' AND '.join(clauses)}", tuple(params)
		).fetchone()
	return _with_state(dict(row)) if row else None


def tenant_for(email: str) -> dict | None:
	"""Tenant lookup memoised per request; permission hooks run this for every query."""
	key = cstr(email).strip().lower()
	if not key or key in ("Guest", "Administrator"):
		return None
	memo = _tenant_memo()
	if key not in memo:
		memo[key] = get_tenant(email=key)
	return memo[key]


def my_tenant() -> dict | None:
	"""Tenant of the logged-in user, memoised per request (the save gate runs a lot)."""
	return tenant_for(frappe.session.user)


def list_tenants(
	search: str | None = None,
	status: str | None = None,
	start: int = 0,
	page_len: int = 50,
) -> dict:
	clauses, params = [], []
	if search:
		like = f"%{cstr(search).strip().lower()}%"
		clauses.append("(lower(owner_email) LIKE ? OR lower(company_name) LIKE ?)")
		params += [like, like]
	if status and status in TENANT_STATUSES:
		clauses.append("status = ?")
		params.append(status)
	where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

	with _db() as conn:
		total = conn.execute(f"SELECT COUNT(*) FROM tenants {where}", tuple(params)).fetchone()[0]
		rows = conn.execute(
			f"""SELECT * FROM tenants {where}
                 ORDER BY datetime(trial_end) DESC, id DESC
                 LIMIT ? OFFSET ?""",
			(*params, cint(page_len) or 50, cint(start)),
		).fetchall()

	tenants = []
	for row in rows:
		tenant = _with_state(dict(row))
		tenant["effective_status"] = "blocked" if tenant["blocked"] else tenant["status"]
		tenants.append(tenant)
	return {"tenants": tenants, "total": total}


def extend_trial(tenant_id: int, days: int | None = None, until: str | None = None, reason: str | None = None) -> dict:
	tenant = get_tenant(tenant_id=tenant_id)
	if not tenant:
		frappe.throw(_("No tenant with id {0}").format(tenant_id))

	if until:
		until = cstr(until).replace("T", " ").strip()
		if len(until) == 10:  # a bare date means "through the end of that day"
			until += " 23:59:59"
		try:
			new_end = _parse(until)
		except ValueError:
			frappe.throw(_("Enter a valid expiry date (YYYY-MM-DD)"))
	elif days:
		base = max(_parse(tenant["trial_end"]), now_datetime())
		new_end = base + timedelta(days=cint(days))
	else:
		frappe.throw(_("Provide either days or an expiry date"))

	# otherwise an extension would move trial_end into the future and the tenant stays blocked
	status = "trial" if tenant["status"] == "expired" and new_end > now_datetime() else tenant["status"]

	stamp = _ts(now_datetime())
	with _db() as conn:
		conn.execute(
			"UPDATE tenants SET trial_end = ?, status = ?, updated_at = ? WHERE id = ?",
			(_ts(new_end), status, stamp, tenant_id),
		)
		_log_history(
			conn,
			tenant_id,
			"extended",
			from_end=tenant["trial_end"],
			to_end=_ts(new_end),
			days=cint(days),
			reason=reason,
		)

	_reset_tenant_memo()
	return get_tenant(tenant_id=tenant_id)


def set_status(tenant_id: int, status: str, reason: str | None = None) -> dict:
	if status not in TENANT_STATUSES:
		frappe.throw(_("Status must be one of {0}").format(", ".join(TENANT_STATUSES)))
	tenant = get_tenant(tenant_id=tenant_id)
	if not tenant:
		frappe.throw(_("No tenant with id {0}").format(tenant_id))

	now = _ts(now_datetime())
	# marking a tenant expired must close the window too, else the stored trial_end
	# still reads as a future date while the tenant is already blocked
	trial_end = now if status == "expired" and _parse(tenant["trial_end"]) > now_datetime() else tenant["trial_end"]

	with _db() as conn:
		conn.execute(
			"UPDATE tenants SET status = ?, trial_end = ?, updated_at = ? WHERE id = ?",
			(status, trial_end, now, tenant_id),
		)
		_log_history(
			conn,
			tenant_id,
			"status_changed",
			from_end=tenant["trial_end"],
			to_end=trial_end,
			reason=f"{tenant['status']} -> {status}: {reason or ''}",
		)

	_reset_tenant_memo()
	return get_tenant(tenant_id=tenant_id)


def history(tenant_id: int, limit: int = 20) -> list[dict]:
	with _db() as conn:
		rows = conn.execute(
			"""SELECT action, from_end, to_end, days, reason, performed_by, created_at
               FROM trial_history WHERE tenant_id = ?
               ORDER BY id DESC LIMIT ?""",
			(cint(tenant_id), cint(limit)),
		).fetchall()
	return [dict(row) for row in rows]


def _require_admin() -> None:
	roles = frappe.get_roles()
	if ADMIN_ROLE not in roles and "System Manager" not in roles:
		frappe.throw(_("Not permitted"), frappe.PermissionError)


@frappe.whitelist()
def get_my_subscription() -> dict:
	tenant = my_tenant()
	if not tenant:
		return {"is_tenant": False}
	return {"is_tenant": True, **tenant}


def add_bootinfo(bootinfo) -> None:
	tenant = my_tenant()
	bootinfo.onerp_subscription = {"is_tenant": bool(tenant), **(tenant or {})}


def user_permission_query_conditions(user: str | None = None, doctype: str | None = None) -> str:
	"""permission_query_conditions "User": a tenant sees only their own user row.

	Tenant roles hold `select` on User but not `read`, so the exposure is the list
	itself -- list view, reports, exports and User link searches, all of which run
	through this condition. Frappe's own condition (hiding Administrator/Guest) is
	ANDed with this one, and users without a tenant row are left alone.
	"""
	if not user:
		user = frappe.session.user
	if not tenant_for(user):
		return ""
	return f"`tabUser`.name = {frappe.db.escape(user)}"


def validate_tenant_active(doc, method=None) -> None:
	"""doc_events "*": the tenancy gate.

	An expired or suspended tenant cannot touch business data, and no tenant may open a
	second company -- Accounts Manager alone would allow it from the desk.

	Frappe housekeeping (login Activity Log, email queue, desk settings) is left alone --
	blocking it would break login before the expired screen could even render.
	"""
	if getattr(doc.meta, "module", None) in INFRA_MODULES:
		return
	tenant = my_tenant()
	if not tenant:
		return

	# is_new() is not reliable here (a dict-built doc has no __islocal flag), so compare
	# against the tenant's own company: anything else is a second company or a rename.
	if doc.doctype == "Company" and cstr(doc.name) != cstr(tenant["company_name"]):
		frappe.throw(
			_("An Onerp account owns one company. Contact sales to add another."),
			frappe.PermissionError,
		)

	if tenant["blocked"]:
		# a manually expired/suspended tenant can still have a trial_end in the future,
		# so only quote a date when the window itself has run out
		if tenant["expired"]:
			message = _("Your trial ended on {0}. Contact sales to continue using Onerp.").format(
				format_date(tenant["trial_end"])
			)
		else:
			message = _("Your Onerp subscription is {0}. Contact sales to continue using Onerp.").format(
				_(tenant["status"].title())
			)
		frappe.throw(message, frappe.PermissionError)


@frappe.whitelist()
def admin_list_tenants(search=None, status=None, start=0, page_len=50) -> dict:
	_require_admin()
	return list_tenants(search=search, status=status, start=start, page_len=page_len)


@frappe.whitelist()
def admin_get_tenant(tenant_id) -> dict:
	_require_admin()
	tenant = get_tenant(tenant_id=tenant_id)
	if not tenant:
		frappe.throw(_("Tenant not found"))
	return {"tenant": tenant, "history": history(tenant["id"])}


@frappe.whitelist()
def admin_extend_trial(tenant_id, days=None, until=None, reason=None) -> dict:
	_require_admin()
	return extend_trial(tenant_id, days=days, until=until, reason=reason)


@frappe.whitelist()
def admin_set_tenant_status(tenant_id, status, reason=None) -> dict:
	_require_admin()
	return set_status(tenant_id, status, reason=reason)
