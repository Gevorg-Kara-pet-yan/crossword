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

Comparison follows the field's type: numbers compare as numbers, booleans
accept true/false/1/0, lists (e.g. reasons) match when any element matches,
and dates (ISO strings) compare as text — a filter value of just a date
(2026-11-24) compares against the date part only, so lte:2026-11-24
includes that whole day. Empty values sort last in both directions.

An unknown field or operator, or a non-number for a numeric field, is a
400 with the list of allowed fields — never silently ignored.

Endpoints without these parameters behave exactly as before (same SQL,
same order, same paging): see `has_query`.
"""

import re

OPS = ("eq", "ne", "contains", "startswith", "gt", "gte", "lt", "lte", "in")
MAX_FILTERS = 20
MAX_VALUE_LEN = 200
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


# ── comparison ──────────────────────────────────────────────────────────────

def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


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
