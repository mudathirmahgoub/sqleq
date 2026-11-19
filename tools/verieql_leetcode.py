#!/usr/bin/env python3
# Unless explicitly stated otherwise all files in this repository are licensed under the
# Apache License Version 2.0.
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Fetch VeriEQL's LeetCode benchmark and write each pair as a file sqleq-check reads.

    python3 tools/verieql_leetcode.py            # into target/verieql/leetcode/
    sqleq-check --expect report-only --cvc5 --qed --sqleq-solver --axes frontend,fuzz \\
        --jsonl target/verieql/leetcode/run.jsonl target/verieql/leetcode/cases

The input is `experiments/2025_10_31/leetcode.out` of https://github.com/VeriEQL/VeriEQL at the
commit pinned below: 23,994 JSON lines, each a pair of queries, the tables they read, the
constraints on those tables, and what VeriEQL answered. VeriEQL's data is licensed CC BY-NC-SA
4.0 (non-commercial use only), so nothing of it is committed here, not even an example: this
script downloads the file, checks its digest, and writes under `target/` only. It reads the file's
JSON and nothing of VeriEQL's code.

Each line becomes `cases/<problem>/<index>.sql`. For a made-up line:

    -- VeriEQL LeetCode pair 0, problem 0.
    -- VeriEQL answered TMO in 600.75s.

    create table "shops" ("id" INTEGER NOT NULL, "city" VARCHAR, PRIMARY KEY ("id"));
    create table "sales" ("id" INTEGER NOT NULL, "shopid" INTEGER NOT NULL, PRIMARY KEY ("id"),
      FOREIGN KEY ("shopid") REFERENCES "shops" ("id"));
    SELECT CITY FROM SHOPS WHERE ID NOT IN (SELECT SHOPID FROM SALES);
    SELECT CITY FROM SHOPS WHERE NOT EXISTS (SELECT 1 FROM SALES WHERE SALES.SHOPID = SHOPS.ID);

The queries are copied as VeriEQL has them (upper-cased, MySQL's dialect), so a MySQL-only
function such as `IFNULL` is the frontend's to refuse. `manifest.jsonl` has one line per pair:
its file, VeriEQL's answer and time, and the constraints that were dropped.

How the schema is written:

| VeriEQL                    | here                                                          |
|----------------------------|---------------------------------------------------------------|
| `INT`, `BOOL`, none        | `INTEGER` (VeriEQL reads all three as integers)               |
| `VARCHAR`, `ENUM,a,b`      | `VARCHAR` (VeriEQL does not hold an enum to its values)       |
| `DATE`, `TIME`, `NUMERIC`  | the same                                                      |
| `primary [a, b]`           | `PRIMARY KEY (a, b)`; a table's later ones `UNIQUE`, NOT NULL |
| `foreign [c, p]`           | `c` NOT NULL, and `FOREIGN KEY (c) REFERENCES t (p)` when `p` is a key of `t` |
| `gt [a, 0]`, `in`, `between` | `CHECK (a > 0)` and `a` NOT NULL                            |
| `lt [a, b]`, `neq [a, b]`, one table | `CHECK (a < b)`, `a` and `b` NOT NULL                 |
| `imply`, `inc`, `consec`   | dropped: no `CREATE TABLE` states them                        |

The NOT NULLs are VeriEQL's: it reads every comparison, a reference included, as false on NULL.
VeriEQL compares two columns over every pair of rows of the table; a `CHECK` compares them within
a row, which is weaker. Every constraint written here is one VeriEQL also assumes, so a proof here
holds under VeriEQL's constraints too; a counterexample here may break one that was dropped or
weakened, which `manifest.jsonl` records.
"""

import argparse
import collections
import hashlib
import json
import os
import re
import sys
import urllib.request

COMMIT = "493cbb81000205e33b0623cfd1c39106fa035fae"
PATH = "experiments/2025_10_31/leetcode.out"
URL = f"https://raw.githubusercontent.com/VeriEQL/VeriEQL/{COMMIT}/{PATH}"
SHA256 = "3a0f67ed9225ea0f6455f8a455f799c21dde6693c1caa03d32bc602fb6ee56c6"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TYPES = {"INT": "INTEGER", "BOOL": "INTEGER", "NONE": "INTEGER", "VARCHAR": "VARCHAR",
         "DATE": "DATE", "TIME": "TIME", "NUMERIC": "NUMERIC"}
COMPARE = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "neq": "<>"}


class Dropped(Exception):
    """A constraint no CREATE TABLE of these tables can state; the message says which."""


def fetch(dest):
    """The input file at `dest`, downloaded first unless a copy with the right digest is there."""
    if not os.path.exists(dest) or sha256(dest) != SHA256:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        print(f"fetching {URL}", file=sys.stderr)
        print("VeriEQL's data is licensed CC BY-NC-SA 4.0, non-commercial use only: "
              "https://creativecommons.org/licenses/by-nc-sa/4.0/", file=sys.stderr)
        with urllib.request.urlopen(URL) as r, open(dest + ".part", "wb") as f:
            f.write(r.read())
        os.replace(dest + ".part", dest)
    got = sha256(dest)
    if got != SHA256:
        sys.exit(f"{dest}: sha256 {got}, expected {SHA256}")
    return dest


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def ident(name):
    """A table or column name, quoted in the lower case an unquoted name in the queries folds to."""
    return '"' + name.lower().replace('"', '""') + '"'


def column(ref):
    """`{"value": "T__C"}` as (table, column), in VeriEQL's upper case."""
    if not (isinstance(ref, dict) and set(ref) == {"value"} and isinstance(ref["value"], str)):
        raise Dropped(f"operand {json.dumps(ref)}")
    table, sep, col = ref["value"].partition("__")
    if not sep:
        raise Dropped(f"operand {json.dumps(ref)}")
    return table, col


def literal(v):
    """A constant operand as SQL."""
    if isinstance(v, bool):
        raise Dropped(f"constant {json.dumps(v)}")
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, dict) and len(v) == 1:
        (kind, s), = v.items()
        if isinstance(s, str) and kind == "literal":
            return "'" + s.replace("'", "''") + "'"
        if isinstance(s, str) and kind == "date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return f"DATE '{s}'"
    raise Dropped(f"constant {json.dumps(v)}")


def show(c):
    return json.dumps(c, separators=(",", ":"))


class Schema:
    def __init__(self, tables):
        # VeriEQL's order of tables and columns, which is the order the DDL is written in.
        self.cols = collections.OrderedDict()
        for t, cols in tables.items():
            self.cols[t] = collections.OrderedDict(
                (c, TYPES.get(str(ty).upper(), "VARCHAR" if str(ty).upper().startswith("ENUM") else None))
                for c, ty in cols.items())
        self.not_null = collections.defaultdict(set)
        self.keys = collections.defaultdict(list)       # table -> [cols], first one primary
        self.checks = collections.defaultdict(list)     # table -> [sql]
        self.fks = collections.defaultdict(list)        # table -> [(col, parent, pcol)]
        self.dropped = []

    def has(self, table, col):
        return table in self.cols and col in self.cols[table]

    def add(self, c):
        try:
            self._add(c)
        except (Dropped, IndexError, KeyError, TypeError, ValueError) as e:
            kind = next(iter(c), "?") if isinstance(c, dict) else "?"
            self.dropped.append(f"{kind} {show(c)} ({e})")

    def _add(self, c):
        if not (isinstance(c, dict) and len(c) == 1):
            raise Dropped("not one constraint")
        (kind, ops), = c.items()
        if kind in ("imply", "inc", "consec"):
            raise Dropped("no CREATE TABLE states it")
        if kind == "primary":
            cols = [column(o) for o in ops]
            tables = {t for t, _ in cols}
            if len(tables) != 1 or not all(self.has(t, k) for t, k in cols):
                raise Dropped("columns of no one table")
            t = tables.pop()
            key = [k for _, k in cols]
            if key not in self.keys[t]:
                self.keys[t].append(key)
            self.not_null[t].update(key)
            return
        if kind == "foreign":
            (ct, cc), (pt, pc) = column(ops[0]), column(ops[1])
            if not (self.has(ct, cc) and self.has(pt, pc)):
                raise Dropped("an unknown column")
            # VeriEQL's reference is an equality, which is false on NULL: the child is never NULL,
            # whether or not the reference itself can be stated.
            self.not_null[ct].add(cc)
            if [pc] not in self.keys[pt]:
                raise Dropped(f"{pt}.{pc} is not a key of its own; {ct}.{cc} is still NOT NULL")
            child, parent = self.cols[ct][cc], self.cols[pt][pc]
            if child != parent:
                raise Dropped(f"{child} references {parent}")
            self.fks[ct].append((cc, pt, pc))
            return
        if kind in COMPARE or kind in ("in", "between"):
            t, a = column(ops[0])
            if not self.has(t, a):
                raise Dropped("an unknown column")
            if kind == "in":
                choices = ops[1]
                if not (isinstance(choices, list) and choices):
                    raise Dropped("no choices")
                sql = f"{ident(a)} IN ({', '.join(literal(v) for v in choices)})"
                nn = [a]
            elif kind == "between":
                sql = f"{ident(a)} BETWEEN {literal(ops[1])} AND {literal(ops[2])}"
                nn = [a]
            elif isinstance(ops[1], dict) and "value" in ops[1]:
                t2, b = column(ops[1])
                if t2 != t or not self.has(t2, b):
                    raise Dropped("columns of two tables")
                sql = f"{ident(a)} {COMPARE[kind]} {ident(b)}"
                nn = [a, b]
            else:
                sql = f"{ident(a)} {COMPARE[kind]} {literal(ops[1])}"
                nn = [a]
            self.checks[t].append(sql)
            self.not_null[t].update(nn)
            return
        raise Dropped(f"unknown kind {kind!r}")

    def ddl(self):
        """CREATE TABLEs, each after the tables it references."""
        order, done, active = [], set(), set()

        def visit(t):
            active.add(t)
            for fk in list(self.fks[t]):
                cc, pt, pc = fk
                if pt == t or pt in done:
                    continue
                if pt in active:
                    self.fks[t].remove(fk)
                    self.dropped.append(f"foreign {t}.{cc} -> {pt}.{pc} (a cycle of references)")
                else:
                    visit(pt)
            active.discard(t)
            done.add(t)
            order.append(t)

        for t in self.cols:
            if t not in done:
                visit(t)
        out = []
        for t in order:
            items = [f"{ident(c)} {ty or 'INTEGER'}" + (" NOT NULL" if c in self.not_null[t] else "")
                     for c, ty in self.cols[t].items()]
            for i, key in enumerate(self.keys[t]):
                items.append(("PRIMARY KEY" if i == 0 else "UNIQUE") + f" ({', '.join(map(ident, key))})")
            for cc, pt, pc in self.fks[t]:
                items.append(f"FOREIGN KEY ({ident(cc)}) REFERENCES {ident(pt)} ({ident(pc)})")
            for sql in self.checks[t]:
                items.append(f"CHECK ({sql})")
            out.append(f"create table {ident(t)} (\n  " + ",\n  ".join(items) + "\n);")
        return "\n".join(out)


def problem(d):
    m = re.search(r"(\d+)\.csv$", d.get("file") or "")
    return m.group(1) if m else "unknown"


def statement(q):
    return q.strip().rstrip(";").rstrip() + ";"


def convert(d):
    """The pair file for one line, and its manifest entry."""
    schema = Schema(d.get("schema") or {})
    # Keys first: a reference is stated only to a key, which VeriEQL may list after it.
    for c in sorted(d.get("constraint") or [], key=lambda c: not (isinstance(c, dict) and "primary" in c)):
        schema.add(c)
    ddl = schema.ddl()
    states = d.get("states") or []
    verdict = states[-1] if states else "none"
    seconds = sum(x for t in d.get("times") or [] if isinstance(t, list) for x in t
                  if isinstance(x, (int, float)))
    head = [f"-- VeriEQL LeetCode pair {d['index']}, problem {problem(d)}.",
            f"-- VeriEQL answered {verdict} in {seconds:.2f}s."]
    if schema.dropped:
        head.append("-- Constraints VeriEQL assumes that this schema does not state:")
        head += [f"--   {x}" for x in schema.dropped]
    body = "\n".join(head) + "\n\n" + ddl + "\n" + "\n".join(statement(q) for q in d["pair"]) + "\n"
    entry = {"index": d["index"], "problem": problem(d), "verieql": verdict,
             "verieql_seconds": round(seconds, 6), "dropped": schema.dropped}
    return body, entry


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--input", help="a local leetcode.out to read instead of downloading one")
    ap.add_argument("--out", default=os.path.join(ROOT, "target", "verieql", "leetcode"),
                    help="where cases/ and manifest.jsonl go (default: target/verieql/leetcode)")
    args = ap.parse_args()
    src = args.input or fetch(os.path.join(args.out, "leetcode.out"))
    if args.input and sha256(src) != SHA256:
        sys.exit(f"{src} is not leetcode.out at VeriEQL {COMMIT[:7]}")
    cases = os.path.join(args.out, "cases")
    os.makedirs(args.out, exist_ok=True)
    n, dropped = 0, collections.Counter()
    with open(src) as f, open(os.path.join(args.out, "manifest.jsonl"), "w") as man:
        for line in f:
            d = json.loads(line)
            body, entry = convert(d)
            rel = os.path.join(entry["problem"], f"{d['index']:05d}.sql")
            entry["case"] = rel
            os.makedirs(os.path.join(cases, entry["problem"]), exist_ok=True)
            with open(os.path.join(cases, rel), "w") as out:
                out.write(body)
            man.write(json.dumps(entry) + "\n")
            n += 1
            dropped.update(x.split(" ")[0] for x in entry["dropped"])
    print(f"wrote {n} pairs under {cases}", file=sys.stderr)
    if dropped:
        print("dropped constraints: " + ", ".join(f"{k} {v}" for k, v in dropped.most_common()),
              file=sys.stderr)


if __name__ == "__main__":
    main()
