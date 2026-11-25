"""
admin_query.py — sorting and filtering for the admin list endpoints.

One contract, shared by every product the AuthServer admin panel manages
(Crossword, PairMatch, FlipBook, Learn), so the panel can build the same
query string for any table:

    ?sort=<field>&dir=asc|desc
    &filter=<field>:<op>:<value>        (repeatable; all filters must match)

  <field>  the JSON field name exactly as the endpoint returns it
           (camelCase: viewCount, creatorName, createdAt ...)
  <op>     eq | ne | contains | startswith | gt | gte | lt | lte | in
           eq/ne/contains/startswith are case-insensitive;
           in takes values separated by "|"  (status:in:Hidden|Visible)

  Examples
    /api/admin/crosswords?filter=viewCount:gt:5&sort=viewCount&dir=desc
    /api/admin/users?filter=creatorName:eq:Artak
    /api/admin/crosswords?filter=createdAt:gte:2026-11-01&filter=subject:eq:Math

Two ways to apply it:

  * `sql_query(args, fields)` — for lists read from the database. Each
    endpoint maps its JSON fields to SQL expressions (`Field`), and this
    returns WHERE conditions, their parameters and an ORDER BY to add to the
    endpoint's own query, before its LIMIT/OFFSET — so the database filters,
    sorts and returns only the requested page. User values are always SQL
    parameters, never part of the SQL text.
  * `apply_request(items, args)` — for lists the endpoint already builds in
    memory (aggregated users, one crossword's results, log files): filters
    the JSON items.

Comparison follows the field's type: numbers compare as numbers, booleans
accept true/false/1/0, lists (e.g. reasons) match when any element matches.
A date value covers the span it names — 2026-11-24 the whole day, 10:05
that minute, 10:05:30 that second — so eq matches what the list shows and
lte:2026-11-24 includes that day. Empty values sort last in both directions.

An unknown field or operator, or a bad value for the field's type, is a
400 with the list of allowed fields — never silently ignored.

Endpoints without these parameters behave exactly as before (same SQL,
same order, same paging): see `has_query`.
"""

import re
from datetime import datetime, timedelta

OPS = ("eq", "ne", "contains", "startswith", "gt", "gte", "lt", "lte", "in")
MAX_FILTERS = 20
MAX_VALUE_LEN = 200
MAX_IN_VALUES = 100
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")


class QueryError(ValueError):
    """Bad sort/filter parameters -> HTTP 400."""

    def __init__(self, message, allowed=None):
        super().__init__(message)
        self.allowed = sorted(allowed) if allowed else []

    def to_dict(self):
        body = {"error": str(self)}
        if self.allowed:
            body["allowedFields"] = self.allowed
        return body


def has_query(args):
    """True when the request asks for sorting or filtering."""
    return bool((args.get("sort") or "").strip()) or any(
        (f or "").strip() for f in args.getlist("filter"))


def parse(args):
    """-> (sort_field or None, descending: bool, [(field, op, value)])"""
    sort = (args.get("sort") or "").strip() or None
    direction = (args.get("dir") or "asc").strip().lower()
    if direction not in ("asc", "desc"):
        raise QueryError("dir must be asc or desc")

    raw = [f for f in args.getlist("filter") if (f or "").strip()]
    if len(raw) > MAX_FILTERS:
        raise QueryError(f"At most {MAX_FILTERS} filters")

    filters = []
    for f in raw:
        parts = f.split(":", 2)
        if len(parts) != 3 or not parts[0].strip():
            raise QueryError(f"Bad filter '{f}', expected field:op:value")
        field, op, value = parts[0].strip(), parts[1].strip().lower(), parts[2]
        if op not in OPS:
            raise QueryError(f"Unknown operator '{op}', expected one of: {', '.join(OPS)}")
        if len(value) > MAX_VALUE_LEN:
            raise QueryError(f"Filter value too long (max {MAX_VALUE_LEN})")
        filters.append((field, op, value.strip()))
    return sort, direction == "desc", filters


def _to_num(field, raw):
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        raise QueryError(f"'{field}' is a number; '{raw}' is not")


def _to_bool(field, raw):
    s = raw.strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    raise QueryError(f"'{field}' is true/false; '{raw}' is not")


# ═════════════════════════════════════════════════════════════════════════════
#  SQL — for lists read from the database
# ═════════════════════════════════════════════════════════════════════════════

TEXT, NUM, BOOL, DATE, LIST = "text", "num", "bool", "date", "list"
_SQL_OP = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


class Field:
    """A JSON field of a list as a SQL expression over the endpoint's query.

    `sql` is written by the endpoint (never user input); `kind` is TEXT, NUM,
    BOOL (an expression that is 0/1), DATE or LIST (a JSON array in a text
    column, e.g. vote_feedback.reasons).
    """
    __slots__ = ("sql", "kind")

    def __init__(self, sql, kind=TEXT):
        self.sql, self.kind = sql, kind


def _fold(v):
    return None if v is None else str(v).casefold()


def _escape_like(v):
    return v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("[", "\\[")


def _date_text(dt):
    """Same shape as the stored text: 2026-11-24 10:05:30[.5]."""
    s = dt.strftime("%Y-%m-%d %H:%M:%S")
    if dt.microsecond:
        s += (".%06d" % dt.microsecond).rstrip("0")
    return s


class _SQLite:
    """SQLite: dates are ISO text; case-insensitive compare through adm_fold()
    (Python casefold, so Armenian folds too — SQLite's LOWER() is ASCII only)."""

    def fold(self, e):
        return f"adm_fold({e})"

    def fold_value(self, v):
        return v.casefold()

    def num_text(self, e):
        return f"CAST({e} AS TEXT)"

    def contains(self, e, v):
        return f"instr({e}, ?) > 0", [v]

    def startswith(self, e, v):
        return f"substr({e}, 1, ?) = ?", [len(v), v]

    def date_expr(self, e):
        return f"replace({e}, 'T', ' ')"

    def date_empty(self, e):
        return f"COALESCE({e}, '') = ''"

    def date_param(self, dt):
        return _date_text(dt)

    def list_items(self, e):
        arr = (f"CASE WHEN {e} IS NULL OR {e} = '' THEN '[]' "
               f"WHEN json_valid({e}) AND json_type({e}) = 'array' THEN {e} "
               f"WHEN json_valid({e}) THEN json_array(json_extract({e}, '$')) "
               f"ELSE json_array({e}) END")
        return f"json_each({arr}) AS j"


class _MSSQL:
    """SQL Server: datetime columns; text compares with the column collation
    (case-insensitive on our databases)."""

    def fold(self, e):
        return e

    def fold_value(self, v):
        return v

    def num_text(self, e):
        return f"CAST({e} AS NVARCHAR(50))"

    def contains(self, e, v):
        return f"{e} LIKE ? ESCAPE '\\'", ["%" + _escape_like(v) + "%"]

    def startswith(self, e, v):
        return f"{e} LIKE ? ESCAPE '\\'", [_escape_like(v) + "%"]

    def date_expr(self, e):
        return e

    def date_empty(self, e):
        return f"{e} IS NULL"

    def date_param(self, dt):
        return dt

    def list_items(self, e):
        raise QueryError("list fields are not supported here")


_DIALECTS = {"sqlite": _SQLite(), "mssql": _MSSQL()}


def register(conn):
    """SQLite: add adm_fold() to the connection (used by text filters/sorts)."""
    conn.create_function("adm_fold", 1, _fold, deterministic=True)


def _span(field, raw):
    """-> (start, end) of the span a date value names."""
    text = raw.strip()
    try:
        if _DATE_ONLY.match(text):
            start = datetime.strptime(text, "%Y-%m-%d")
            return start, start + timedelta(days=1)
        start = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise QueryError(f"'{field}' is a date; '{raw}' is not (use 2026-11-24 or 2026-11-24 10:05)")
    start = start.replace(tzinfo=None)     # stored values are wall-clock times
    time = re.split(r"[+\-zZ]", text[11:], maxsplit=1)[0] if len(text) > 11 else ""
    if "." in time:
        step = timedelta(microseconds=1)
    elif time.count(":") >= 2:
        step = timedelta(seconds=1)
    else:
        step = timedelta(minutes=1)
    return start, start + step


def _cond(d, field, f, op, raw):
    """-> (sql, params) for one filter."""
    if op == "in":
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) > MAX_IN_VALUES:
            raise QueryError(f"At most {MAX_IN_VALUES} values for in")
        pieces = [_cond(d, field, f, "eq", p) for p in parts]
        return "(" + " OR ".join(s for s, _ in pieces) + ")", [x for _, ps in pieces for x in ps]

    if f.kind == BOOL:
        if op not in ("eq", "ne"):
            raise QueryError(f"'{field}' is true/false; use eq or ne")
        return f"(({f.sql}) {_SQL_OP[op]} ?)", [1 if _to_bool(field, raw) else 0]

    if f.kind == NUM:
        if op in ("contains", "startswith"):
            sql, params = getattr(d, op)(d.num_text(f.sql), raw)
            return f"({sql})", params
        n = _to_num(field, raw)
        return f"(({f.sql}) {_SQL_OP[op]} ?)", [int(n) if n.is_integer() else n]

    if f.kind == DATE:
        return _date_cond(d, field, f.sql, op, raw)

    if f.kind == LIST:
        return _list_cond(d, field, f.sql, op, raw)

    e = f"COALESCE({f.sql}, '')"
    if op in ("eq", "ne"):
        sql, params = ((f"{e} = ''", []) if raw == ""
                       else (f"{d.fold(e)} = ?", [d.fold_value(raw)]))
        return (f"({sql})", params) if op == "eq" else (f"NOT ({sql})", params)
    if op in ("contains", "startswith"):
        if raw == "":
            return "(1 = 1)", []
        sql, params = getattr(d, op)(d.fold(e), d.fold_value(raw))
        return f"({sql})", params
    # gt / gte / lt / lte: an empty value never satisfies an ordering comparison
    return f"({e} <> '' AND {d.fold(e)} {_SQL_OP[op]} ?)", [d.fold_value(raw)]


def _date_cond(d, field, sql, op, raw):
    if op in ("contains", "startswith"):
        raise QueryError(f"'{field}' is a date; use eq, ne, gt, gte, lt or lte")
    empty = d.date_empty(sql)
    if raw == "":
        if op == "eq":
            return f"({empty})", []
        if op == "ne":
            return f"(NOT ({empty}))", []
        raise QueryError(f"'{field}' is a date; '{op}' needs a value")

    start, end = _span(field, raw)
    e, lo, hi = d.date_expr(sql), d.date_param(start), d.date_param(end)
    if op == "eq":
        return f"(NOT ({empty}) AND {e} >= ? AND {e} < ?)", [lo, hi]
    if op == "ne":
        return f"(({empty}) OR NOT ({e} >= ? AND {e} < ?))", [lo, hi]
    sign, bound = {"gt": (">=", hi), "gte": (">=", lo), "lt": ("<", lo), "lte": ("<", hi)}[op]
    return f"(NOT ({empty}) AND {e} {sign} ?)", [bound]


def _list_cond(d, field, sql, op, raw):
    items = d.list_items(sql)
    value = d.fold("j.value")
    if op in ("eq", "ne"):
        exists = f"EXISTS (SELECT 1 FROM {items} WHERE {value} = ?)"
        return (f"({exists})" if op == "eq" else f"(NOT {exists})"), [d.fold_value(raw)]
    if op in ("contains", "startswith"):
        test, params = getattr(d, op)(value, d.fold_value(raw))
        return f"(EXISTS (SELECT 1 FROM {items} WHERE {test}))", params
    raise QueryError(f"'{field}' is a list; use eq, ne, in, contains or startswith")


def _order(d, f, desc):
    direction = " DESC" if desc else ""
    if f.kind in (NUM, BOOL):
        return f"({f.sql}){direction}"
    if f.kind == DATE:
        return (f"CASE WHEN {d.date_empty(f.sql)} THEN 1 ELSE 0 END, "
                f"{d.date_expr(f.sql)}{direction}")
    sql = (f"(SELECT group_concat(j.value, ', ') FROM {d.list_items(f.sql)})"
           if f.kind == LIST else f.sql)
    e = f"COALESCE({sql}, '')"
    return f"CASE WHEN {e} = '' THEN 1 ELSE 0 END, {d.fold(e)}{direction}"


class SqlQuery:
    """What an endpoint adds to its own query: `where` (conditions, all
    AND-ed), `params` (for those conditions, in order) and `order_by()`."""

    def __init__(self, where, params, order):
        self.where, self.params, self.order = where, params, order

    def order_by(self, default):
        """ORDER BY text: the ?sort terms, then the endpoint's order as tie-break."""
        return f"{self.order}, {default}" if self.order else default


def sql_query(args, fields, dialect="sqlite"):
    """None when the request has no sort/filter; otherwise a SqlQuery built
    from `fields` ({json name: Field}). Raises QueryError (-> 400)."""
    if not has_query(args):
        return None
    sort, desc, filters = parse(args)
    d = _DIALECTS[dialect]

    def field(name, what):
        if name not in fields:
            raise QueryError(f"Unknown {what} field '{name}'", fields.keys())
        return fields[name]

    where, params = [], []
    for name, op, raw in filters:
        sql, ps = _cond(d, name, field(name, "filter"), op, raw)
        where.append(sql)
        params += ps
    order = _order(d, field(sort, "sort"), desc) if sort else None
    return SqlQuery(where, params, order)


# ═════════════════════════════════════════════════════════════════════════════
#  In memory — for lists the endpoint already builds in Python
# ═════════════════════════════════════════════════════════════════════════════

def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _cmp(a, b):
    return (a > b) - (a < b)


def _match_scalar(field, v, op, raw):
    if op == "in":
        return any(_match_scalar(field, v, "eq", part) for part in raw.split("|"))

    if isinstance(v, bool):
        if op not in ("eq", "ne"):
            raise QueryError(f"'{field}' is true/false; use eq or ne")
        same = v == _to_bool(field, raw)
        return same if op == "eq" else not same

    if _is_num(v):
        if op in ("contains", "startswith"):
            s = str(v)
            return raw in s if op == "contains" else s.startswith(raw)
        c = _cmp(float(v), _to_num(field, raw))
    else:
        s = "" if v is None else str(v)
        left, right = s.casefold(), raw.casefold()
        if op == "contains":
            return right in left
        if op == "startswith":
            return left.startswith(right)
        # A bare date compares with the date part only, so lte:2026-11-24
        # includes 2026-11-24 15:30.
        if _DATE_ONLY.match(raw) and _DATE_PREFIX.match(s):
            left = left[:10]
        if v is None or s == "":
            # Empty never satisfies an ordering comparison.
            if op in ("gt", "gte", "lt", "lte"):
                return False
        c = _cmp(left, right)

    return {"eq": c == 0, "ne": c != 0, "gt": c > 0, "gte": c >= 0,
            "lt": c < 0, "lte": c <= 0}[op]


def _match(field, v, op, raw):
    if isinstance(v, (list, tuple)):
        if op == "ne":
            return not any(_match_scalar(field, x, "eq", raw) for x in v)
        return any(_match_scalar(field, x, op, raw) for x in v)
    return _match_scalar(field, v, op, raw)


def _sort_key(v):
    """(empty?, type rank, value) — empties last, numbers numeric, text casefolded."""
    if v is None or v == "" or v == []:
        return (1, 0, 0)
    if isinstance(v, bool):
        return (0, 0, int(v))
    if _is_num(v):
        return (0, 0, float(v))
    if isinstance(v, (list, tuple)):
        v = ", ".join(str(x) for x in v)
    return (0, 1, str(v).casefold())


def apply(items, sort=None, desc=False, filters=()):
    """Filter then sort a list of dicts (the endpoint's JSON items)."""
    if not items or (not sort and not filters):
        return list(items)

    allowed = set(items[0].keys())
    for field, _, _ in filters:
        if field not in allowed:
            raise QueryError(f"Unknown filter field '{field}'", allowed)
    if sort and sort not in allowed:
        raise QueryError(f"Unknown sort field '{sort}'", allowed)

    out = [it for it in items
           if all(_match(f, it.get(f), op, val) for f, op, val in filters)]

    if sort:
        filled = [it for it in out if _sort_key(it.get(sort))[0] == 0]
        empty = [it for it in out if _sort_key(it.get(sort))[0] == 1]
        filled.sort(key=lambda it: _sort_key(it.get(sort))[1:], reverse=desc)
        out = filled + empty
    return out


def apply_request(items, args):
    sort, desc, filters = parse(args)
    return apply(items, sort, desc, filters)


def page_slice(items, page, count):
    start = (page - 1) * count
    return items[start:start + count]
