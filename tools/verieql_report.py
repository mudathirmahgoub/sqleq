#!/usr/bin/env python3
# Unless explicitly stated otherwise all files in this repository are licensed under the
# Apache License Version 2.0.
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Summarize a sqleq-check run over the VeriEQL LeetCode pairs, beside VeriEQL's own answers.

    python3 tools/verieql_report.py target/verieql/leetcode/manifest.jsonl sqleq-solver.jsonl \\
        qed.jsonl fuzz.jsonl cvc5.jsonl --timeout 20 --svg cactus.svg

`manifest.jsonl` is what `tools/verieql_leetcode.py` writes, and each run file what `sqleq-check
--jsonl` writes over its `cases/`: one run with every axis, or one run per axis, merged here by
pair. Prints Markdown tables of each axis's answers and of sqleq's answer against VeriEQL's.
`--svg` also draws a cactus plot of each axis's decisive answers: a proof for a prover, a
counterexample for sqleq-fuzz, a model for cvc5. Standard library only.

An alarm is a proof (cvc5's included) beside a counterexample, or a cvc5 model beside a proof of
`sqleq-solver` or QED, whichever runs they came from.
"""

import argparse
import collections
import json
import math
import sys

PROVED = ("proved", "proved-literal")

# Each axis: its label, the bucket of a case, and its own time in seconds.
AXES = {
    "frontend": ("frontend", lambda c: frontend(c), lambda c: c.get("lower_wall")),
    "sqleq-solver": ("sqleq-solver", lambda c: c.get("s_bucket"), lambda c: ms(c.get("s_ms"))),
    "qed": ("QED", lambda c: qed(c), lambda c: c.get("prove_wall")),
    "cvc5": ("cvc5", lambda c: c.get("c_bucket"), lambda c: ms(c.get("c_ms"))),
    "fuzz": ("sqleq-fuzz", lambda c: c.get("f_verdict"), lambda c: ms(c.get("f_ms"))),
}

VERIEQL = {
    "NEQ": "NEQ (counterexample)",
    "TMO": "TMO (no counterexample in 600s)",
    "NIE": "NIE (not implemented)",
    "NSE": "NSE (not supported)",
    "SYN": "SYN (syntax error)",
    "OTE": "OTE (other error)",
    "OOM": "OOM (out of memory)",
}


def ms(v):
    if isinstance(v, (int, float)):
        return v / 1000.0
    return None


def frontend(c):
    if c.get("reflexive"):
        return "reflexive"
    if c.get("lowered"):
        return "emit-reflexive" if c.get("trivial") and c.get("trivial_basis") == "ir" else "emit"
    if c.get("refuse_kind"):
        return "refuse:" + c["refuse_kind"]
    return c.get("status") or "?"


def qed(c):
    s = c.get("status")
    if s == "provable":
        return "proved-literal" if c.get("trivial") else "proved"
    if s == "unprovable":
        return "no-proof"
    if not c.get("lowered") or c.get("qed_unasked"):
        return "no-plan"
    return s


def proved(c, axis):
    return AXES[axis][1](c) in PROVED


def sqleq_answer(c):
    """One answer per case, from the axes that ran: a proof, a counterexample, both, or neither."""
    proof = any(proved(c, a) for a in ("sqleq-solver", "qed")) or frontend(c) in ("emit-reflexive", "reflexive")
    cvc5_proof = proved(c, "cvc5")
    cex = c.get("f_verdict") == "counterexample"
    if (proof or cvc5_proof) and cex or proof and c.get("c_bucket") == "model":
        return "alarm"
    if proof:
        return "proved"
    if cex:
        return "counterexample"
    if cvc5_proof:
        return "proved by cvc5 only"
    if not c.get("lowered") and not c.get("reflexive"):
        return "refused"
    return "undecided"


def table(rows, header):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return "\n".join(out)


def read_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# The fields each axis writes, for merging runs that each asked some of the axes.
FIELDS = {
    "qed": ("status", "prove_wall", "qed_unasked", "smt_timed_out", "q_tail", "q_rc"),
    "sqleq-solver": ("s_bucket", "s_verdict", "s_ms", "s_note", "s_raw"),
    "cvc5": ("c_bucket", "c_verdict", "c_ms", "c_note", "c_raw"),
    "fuzz": ("f_verdict", "f_note", "f_ms", "f_raw"),
}


def asked(c, axis):
    if axis == "qed":
        return c.get("status") not in ("lowered", "refused") or bool(c.get("qed_unasked"))
    return c.get(FIELDS[axis][0]) is not None


def merge(runs):
    """One record per pair: the frontend's fields from the first run, each axis's from a run that
    asked it."""
    out = {}
    for run in runs:
        for c in run:
            k = case_key(c["name"])
            if k not in out:
                out[k] = dict(c)
                continue
            for axis, fields in FIELDS.items():
                if asked(c, axis) and not asked(out[k], axis):
                    out[k].update({f: c.get(f) for f in fields})
    return out


def case_key(name):
    """`175/00000.sql` from whatever prefix the run's display name carries."""
    parts = name.replace("\\", "/").split("/")
    return "/".join(parts[-2:])


def report(manifest, by, axes, timeout):
    rows = [(m, by[m["case"]]) for m in manifest if m["case"] in by]
    print(f"{len(rows)} of {len(manifest)} pairs in the run.\n")

    print("## Each axis\n")
    for a in axes:
        label, bucket, _ = AXES[a]
        n = collections.Counter(bucket(c) or "missing" for _, c in rows)
        print(f"**{label}**: " + ", ".join(f"{k} {v}" for k, v in n.most_common()) + "\n")

    print("## sqleq against VeriEQL\n")
    cols = ["proved", "proved by cvc5 only", "counterexample", "alarm", "undecided", "refused"]
    grid = collections.defaultdict(collections.Counter)
    for m, c in rows:
        grid[m["verieql"]][sqleq_answer(c)] += 1
    body = []
    for v in sorted(grid, key=lambda v: -sum(grid[v].values())):
        body.append([VERIEQL.get(v, v)] + [grid[v][k] for k in cols] + [sum(grid[v].values())])
    total = collections.Counter()
    for g in grid.values():
        total.update(g)
    body.append(["**all**"] + [f"**{total[k]}**" for k in cols] + [f"**{sum(total.values())}**"])
    print(table(body, ["VeriEQL"] + cols + ["pairs"]) + "\n")

    odd = [(m, c) for m, c in rows if m["verieql"] == "NEQ" and sqleq_answer(c) in ("proved", "alarm")]
    if odd:
        print("### Proved here, refuted by VeriEQL\n")
        print(table([[m["case"], ", ".join(a for a in ("sqleq-solver", "qed", "cvc5") if proved(c, a)),
                      c.get("f_verdict") or "", len(m["dropped"])] for m, c in odd],
                    ["pair", "proved by", "sqleq-fuzz", "dropped constraints"]) + "\n")
    alarms = [(m, c) for m, c in rows if sqleq_answer(c) == "alarm"]
    if alarms:
        print("### Alarms\n")
        print(table([[m["case"], m["verieql"], len(m["dropped"])] for m, c in alarms],
                    ["pair", "VeriEQL", "dropped constraints"]) + "\n")

    print("## Decisive answers within the time limit\n")
    series = curves(rows, axes)
    print(table([[name, "proof" if panel == "proofs" else ("model" if name == "cvc5" else "counterexample"),
                  len(ts), f"{sum(ts):.1f}", f"{median(ts):.3f}" if ts else ""]
                 for name, panel, ts in series], ["axis", "answer", "pairs", "total s", "median s"]))
    print(f"\nVeriEQL's NEQ, from its own run (600s limit, other hardware): "
          f"{sum(1 for m, _ in rows if m['verieql'] == 'NEQ')} pairs.")
    return series


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0.0


def curves(rows, axes):
    """(name, panel, sorted times) for each decisive answer an axis gives."""
    want = [("sqleq-solver", "proofs", PROVED, "sqleq-solver"), ("qed", "proofs", PROVED, "QED"),
            ("cvc5", "proofs", PROVED, "cvc5"), ("fuzz", "refutations", ("counterexample",), "sqleq-fuzz"),
            ("cvc5", "refutations", ("model",), "cvc5")]
    out = []
    for axis, panel, ok, name in want:
        if axis not in axes:
            continue
        _, bucket, secs = AXES[axis]
        ts = sorted(max(secs(c) or 0.0, 1e-3) for _, c in rows if bucket(c) in ok)
        out.append((name, panel, ts))
    return out


# Categorical slots 1-4 of the reference palette (light, dark); an axis keeps its color in both panels.
COLORS = {"sqleq-solver": ("#2a78d6", "#3987e5"), "QED": ("#eb6834", "#d95926"),
          "cvc5": ("#1baf7a", "#199e70"), "sqleq-fuzz": ("#eda100", "#c98500")}


def svg(series, timeout, n_pairs, title):
    """Two cactus panels side by side: time on x (log), pairs answered on y."""
    W, H, gap = 980, 420, 40
    pw = (W - gap) / 2
    m = dict(l=64, r=118, t=58, b=52)
    lo = 1e-3
    hi = max([timeout] + [t for _, _, ts in series for t in ts])
    css = ["svg{font-family:system-ui,-apple-system,'Segoe UI',sans-serif}",
           ".bg{fill:#fcfcfb}.t1{fill:#0b0b0b}.t2{fill:#52514e}.mu{fill:#898781}",
           ".grid{stroke:#e1e0d9;stroke-width:1}.axis{stroke:#c3c2b7;stroke-width:1}",
           ".limit{stroke:#898781;stroke-width:1;stroke-dasharray:4 3}"]
    dark = [".bg{fill:#1a1a19}.t1{fill:#f0efec}.t2{fill:#c3c2b7}",
            ".grid{stroke:#2c2c2a}.axis{stroke:#383835}"]
    for i, (name, (light, dk)) in enumerate(COLORS.items()):
        css.append(f".s{i}{{stroke:{light};fill:none;stroke-width:2;stroke-linejoin:round}}"
                   f".f{i}{{fill:{light};stroke:#fcfcfb;stroke-width:2}}")
        dark.append(f".s{i}{{stroke:{dk}}}.f{i}{{fill:{dk};stroke:#1a1a19}}")
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
           f'role="img" aria-label="{esc(title)}">',
           "<style>" + "".join(css) + "@media (prefers-color-scheme: dark){" + "".join(dark) + "}</style>",
           f'<rect class="bg" width="{W}" height="{H}" rx="8"/>',
           f'<text class="t1" x="16" y="26" font-size="15" font-weight="600">{esc(title)}</text>',
           f'<text class="t2" x="16" y="44" font-size="12">Pairs each axis answers decisively within a '
           f'given time ({n_pairs} pairs, {fmt_s(timeout)} limit per case). Time on a log scale.</text>']
    for p, (panel, heading) in enumerate((("proofs", "Proofs"), ("refutations", "Counterexamples and models"))):
        x0 = p * (pw + gap)
        L, R, T, B = x0 + m["l"], x0 + pw - m["r"], m["t"] + 18, H - m["b"]
        ymax = nice(max([1] + [len(ts) for _, pan, ts in series if pan == panel]))

        def X(t):
            return L + (math.log10(max(t, lo)) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)) * (R - L)

        def Y(n):
            return B - n / ymax * (B - T)

        out.append(f'<text class="t1" x="{L}" y="{T - 10}" font-size="13" font-weight="600">{heading}</text>')
        for k in range(5):
            v = ymax * k / 4
            out.append(f'<line class="grid" x1="{L}" x2="{R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>')
            out.append(f'<text class="mu" x="{L - 8}" y="{Y(v) + 4:.1f}" font-size="11" '
                       f'text-anchor="end">{int(v):,}</text>')
        e = math.ceil(math.log10(lo))
        while 10 ** e <= hi * 1.0001:
            out.append(f'<text class="mu" x="{X(10 ** e):.1f}" y="{B + 18}" font-size="11" '
                       f'text-anchor="middle">{fmt_s(10 ** e)}</text>')
            e += 1
        out.append(f'<line class="axis" x1="{L}" x2="{R}" y1="{B}" y2="{B}"/>')
        out.append(f'<line class="limit" x1="{X(timeout):.1f}" x2="{X(timeout):.1f}" y1="{T}" y2="{B}"/>')
        out.append(f'<text class="mu" x="{X(timeout) - 4:.1f}" y="{T + 10}" font-size="11" '
                   f'text-anchor="end">limit {fmt_s(timeout)}</text>')
        out.append(f'<text class="t2" x="{(L + R) / 2:.1f}" y="{H - 14}" font-size="12" '
                   f'text-anchor="middle">time per pair (s)</text>')
        if p == 0:
            out.append(f'<text class="t2" x="16" y="{(T + B) / 2:.1f}" font-size="12" text-anchor="middle" '
                       f'transform="rotate(-90 16 {(T + B) / 2:.1f})">pairs answered</text>')
        labels = []
        for name, pan, ts in series:
            if pan != panel or not ts:
                continue
            i = list(COLORS).index(name)
            pts = " ".join(f"{x},{y}" for x, y in steps([(X(t), Y(k + 1)) for k, t in enumerate(ts)]))
            out.append(f'<polyline class="s{i}" points="{X(ts[0]):.1f},{B} {pts}"><title>{esc(name)}: '
                       f'{len(ts)} pairs</title></polyline>')
            out.append(f'<circle class="f{i}" cx="{X(ts[-1]):.1f}" cy="{Y(len(ts)):.1f}" r="4"/>')
            labels.append([Y(len(ts)), f"{name} {len(ts):,}", X(ts[-1])])
        labels.sort()
        for k in range(1, len(labels)):
            labels[k][0] = max(labels[k][0], labels[k - 1][0] + 15)
        for y, text, x in labels:
            out.append(f'<text class="t1" x="{x + 8:.1f}" y="{y + 4:.1f}" '
                       f'font-size="12">{esc(text)}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def steps(points):
    """The points of a cactus curve, rounded to 0.1px, keeping only the first and the last of those
    that share an x: the same line, in a fraction of the bytes."""
    out = []
    for x, y in points:
        p = (f"{x:.1f}", f"{y:.1f}")
        if len(out) >= 2 and out[-1][0] == out[-2][0] == p[0]:
            out[-1] = p
        elif not out or out[-1] != p:
            out.append(p)
    return out


def nice(n):
    """The smallest 4 × (1, 1.5, 2, 2.5 or 5 × 10^k) that is at least n: four even gridlines."""
    p = 10 ** max(0, math.floor(math.log10(max(n / 4, 1))))
    for f in (1, 1.5, 2, 2.5, 5, 10):
        if 4 * f * p >= n and (f * p) == int(f * p):
            return int(4 * f * p)
    return n


def fmt_s(t):
    if t >= 1:
        return f"{t:g}s"
    return f"{t * 1000:g}ms"


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("manifest")
    ap.add_argument("run", nargs="+")
    ap.add_argument("--timeout", type=float, default=60.0, help="the run's --timeout, in seconds")
    ap.add_argument("--axes", default="frontend,sqleq-solver,qed,cvc5,fuzz")
    ap.add_argument("--svg", help="also write a cactus plot here")
    ap.add_argument("--title", default="sqleq on VeriEQL's LeetCode pairs")
    args = ap.parse_args()
    axes = [a for a in args.axes.split(",") if a]
    for a in axes:
        if a not in AXES:
            sys.exit(f"unknown axis {a!r}")
    manifest = read_jsonl(args.manifest)
    by = merge(read_jsonl(r) for r in args.run)
    series = report(manifest, by, axes, args.timeout)
    if args.svg:
        n = sum(1 for m in manifest if m["case"] in by)
        with open(args.svg, "w") as f:
            f.write(svg(series, args.timeout, f"{n:,}", args.title))


if __name__ == "__main__":
    main()
