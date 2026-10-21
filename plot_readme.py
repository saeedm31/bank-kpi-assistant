"""Draw the README charts (plain SVG, light and dark mode).

Usage:
    python evaluate.py && python plot_readme.py
Writes docs/flow.svg, docs/alert.svg and docs/accuracy.svg. The alert chart uses the evaluation
clock (21 Oct 2026, 14:40), when the simulator plants a card terminal outage in Coimbra.
"""

import datetime as dt
import json
import os

import numpy as np

from bank_assistant import guard
from bank_assistant.db import Bank

W = 900
STYLE = """
  .bg { fill: #fcfcfb; } .grid { stroke: #e4e3df; stroke-width: 1; }
  .axis { fill: #52514e; font: 12px system-ui, -apple-system, sans-serif; }
  .small { fill: #52514e; font: 11.5px system-ui, -apple-system, sans-serif; }
  .title { fill: #0b0b0b; font: 600 15px system-ui, -apple-system, sans-serif; }
  .sub { fill: #0b0b0b; font: 600 13px system-ui, -apple-system, sans-serif; }
  .box { fill: #f3f2ee; stroke: #d6d4cd; stroke-width: 1; } .arrow { stroke: #8a8984; stroke-width: 1.5; fill: none; }
  .s0 { stroke: #2a78d6; } .s1 { stroke: #eb6834; } .f0 { fill: #2a78d6; } .f1 { fill: #eb6834; }
  .f2 { fill: #c9c7bf; } .band { fill: #2a78d6; opacity: .12; } .shade { fill: #eb6834; opacity: .08; }
  .line { fill: none; stroke-width: 2.5; } .dash { stroke-dasharray: 5 4; }
  .white { fill: #ffffff; font: 600 12px system-ui, -apple-system, sans-serif; }
  @media (prefers-color-scheme: dark) {
    .bg { fill: #1a1a19; } .grid { stroke: #34332f; } .axis, .small { fill: #c3c2b7; } .title, .sub { fill: #f0efec; }
    .box { fill: #252523; stroke: #3d3c38; } .arrow { stroke: #8a8984; }
    .s0 { stroke: #3987e5; } .s1 { stroke: #d95926; } .f0 { fill: #3987e5; } .f1 { fill: #d95926; } .f2 { fill: #55544f; }
    .band { fill: #3987e5; }
  }
"""


def svg(height, label, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {height}" width="{W}" height="{height}" '
            f'role="img" aria-label="{label}">\n<style>{STYLE}</style>\n'
            f'<rect class="bg" width="{W}" height="{height}" rx="8"/>\n' + "\n".join(body) + "\n</svg>\n")


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def flow():
    steps = [("1. Question", ["“Card spend in Porto", "vs last month?”"]),
             ("2. Understand", ["KPI: card spend", "Period: 1–21 Oct", "Only: city = Porto", "Compare: 1–21 Sep"]),
             ("3. Write SQL", ["From the agreed KPI", "definition (kpis.yml).", "AI mode: the model", "writes it instead."]),
             ("4. Run safely", ["Read-only, SELECT only,", "known tables only,", "time and row limits."]),
             ("5. Answer", ["€150.0k, down 2.4%,", "+ chart, SQL, data and", "how it read the question."])]
    body = ['<text class="title" x="24" y="30">From question to answer</text>']
    bw, gap, top, h = 156, 22, 52, 150
    for i, (head, lines) in enumerate(steps):
        x = 24 + i * (bw + gap)
        body.append(f'<rect class="box" x="{x}" y="{top}" width="{bw}" height="{h}" rx="8"/>')
        body.append(f'<text class="sub" x="{x + 12}" y="{top + 26}">{esc(head)}</text>')
        for j, line in enumerate(lines):
            body.append(f'<text class="small" x="{x + 12}" y="{top + 52 + 19 * j}">{esc(line)}</text>')
        if i < len(steps) - 1:
            ax = x + bw + 3
            body.append(f'<path class="arrow" d="M{ax} {top + h / 2:.0f} h{gap - 8} m-5 -5 l5 5 l-5 5"/>')
    body.append(f'<text class="small" x="24" y="{top + h + 30}">If the question is outside what it knows (a forecast, '
                f'a “why”, an action), it says so instead of guessing.</text>')
    return svg(top + h + 48, "Steps from a question to an answer", body)


def alert_chart(conn, now, branch="Coimbra"):
    day = now.date()
    weeks = [day - dt.timedelta(weeks=k) for k in range(5)]
    sql = f"""SELECT substr(t.ts, 1, 10) AS d, CAST(substr(t.ts, 12, 2) AS INTEGER) AS h, COUNT(*) AS n
              FROM transactions t JOIN customers c ON c.customer_id = t.customer_id
              JOIN branches b ON b.branch_id = c.branch_id
              WHERE t.type = 'Card payment' AND b.name = '{branch}'
                AND substr(t.ts, 1, 10) IN ({", ".join(f"'{w}'" for w in weeks)})
              GROUP BY d, h"""
    df, _ = guard.run(conn, sql)
    grid = np.zeros((5, 24))
    for _, r in df.iterrows():
        grid[weeks.index(dt.date.fromisoformat(r["d"])), r["h"]] = r["n"]
    cum = np.cumsum(grid, axis=1)                       # payments so far, by end of each hour
    usual = cum[1:].mean(0)
    lo, hi = cum[1:].min(0), cum[1:].max(0)
    hours_done = now.hour + 1
    today = cum[0][:hours_done]
    cum_today = int(grid[0].sum())
    cum_usual = (usual[now.hour - 1] if now.hour else 0) + (grid[1:, now.hour].mean() * now.minute / 60)

    x0, x1, top, bottom = 60, 870, 74, 300
    ymax = hi.max() * 1.1
    px = lambda h: x0 + (h + 1) * (x1 - x0) / 24       # value at the end of hour h
    py = lambda v: bottom - v * (bottom - top) / ymax
    body = [f'<text class="title" x="24" y="28">Alert: card payments by {branch} customers today vs a normal {now:%A}</text>',
            f'<text class="axis" x="24" y="50">{cum_today} payments by {now:%H:%M}, against {cum_usual:.0f} usually '
            f'({100 * (cum_today / cum_usual - 1):+.0f}%). The simulator planted a card terminal outage from 08:00.</text>']
    step = next(st for st in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500) if ymax / st <= 5)
    for v in np.arange(0, ymax, step):
        body.append(f'<line class="grid" x1="{x0}" x2="{x1}" y1="{py(v):.0f}" y2="{py(v):.0f}"/>'
                    f'<text class="axis" x="{x0 - 8}" y="{py(v) + 4:.0f}" text-anchor="end">{v:.0f}</text>')
    nx = x0 + (now.hour + now.minute / 60) * (x1 - x0) / 24
    body.append(f'<rect class="shade" x="{x0 + 8 * (x1 - x0) / 24:.0f}" y="{top}" '
                f'width="{nx - (x0 + 8 * (x1 - x0) / 24):.0f}" height="{bottom - top}"/>')
    band = [f"{px(h):.0f},{py(hi[h]):.0f}" for h in range(24)] + [f"{px(h):.0f},{py(lo[h]):.0f}" for h in range(23, -1, -1)]
    body.append(f'<polygon class="band" points="{x0},{py(0):.0f} {" ".join(band)}"/>')
    body.append(f'<polyline class="line s0 dash" points="{x0},{py(0):.0f} ' +
                " ".join(f"{px(h):.0f},{py(usual[h]):.0f}" for h in range(24)) + '"/>')
    body.append(f'<polyline class="line s1" points="{x0},{py(0):.0f} ' +
                " ".join(f"{px(h):.0f},{py(today[h]):.0f}" for h in range(hours_done - 1)) +
                f' {nx:.0f},{py(cum_today):.0f}"/>')
    for h in range(0, 25, 3):
        body.append(f'<text class="axis" x="{x0 + h * (x1 - x0) / 24:.0f}" y="{bottom + 18}" text-anchor="middle">{h:02d}:00</text>')
    lx = x0 + 14
    body.append(f'<line class="s0 line dash" x1="{lx}" x2="{lx + 24}" y1="{top + 6}" y2="{top + 6}"/>'
                f'<text class="axis" x="{lx + 30}" y="{top + 10}">Usual: last 4 {now:%A}s (range shaded)</text>'
                f'<line class="s1 line" x1="{lx}" x2="{lx + 24}" y1="{top + 26}" y2="{top + 26}"/>'
                f'<text class="axis" x="{lx + 30}" y="{top + 30}">Today, up to {now:%H:%M}</text>')
    body.append(f'<text class="axis" x="{(x0 + x1) / 2:.0f}" y="{bottom + 38}" text-anchor="middle">'
                f'Time of day (payments so far, adding up)</text>')
    return svg(bottom + 52, f"Card payments by {branch} customers per hour today against a normal day", body)


def accuracy_chart(main, unseen):
    rows = [("Main test set", main["in_scope"]["questions"], main["in_scope"]["correct"], 0, 0),
            ("New wording, first try", unseen["in_scope"]["questions"], unseen["in_scope"]["correct"],
             unseen["in_scope"]["declined"], unseen["in_scope"]["wrong"]),
            ("Out of scope (should decline)", main["out_of_scope"]["questions"] + unseen["out_of_scope"]["questions"],
             main["out_of_scope"]["declined"] + unseen["out_of_scope"]["declined"], 0, 0)]
    x0, x1, top, bh, gap = 250, 820, 70, 30, 22
    body = ['<text class="title" x="24" y="28">Built-in engine: right, “I can’t answer that”, or wrong</text>',
            '<text class="axis" x="24" y="50">Questions with hand-written correct SQL; an answer is right only when its numbers match.</text>']
    for i, (name, n, ok, declined, wrong) in enumerate(rows):
        y = top + i * (bh + gap)
        body.append(f'<text class="axis" x="{x0 - 12}" y="{y + bh / 2 + 4:.0f}" text-anchor="end">{esc(name)} ({n})</text>')
        x = x0
        for count, cls, word in ((ok, "f0", "right" if i < 2 else "declined"), (declined, "f2", "declined"),
                                 (wrong, "f1", "wrong")):
            if not count:
                continue
            w = (x1 - x0) * count / n
            body.append(f'<rect class="{cls}" x="{x:.0f}" y="{y}" width="{w:.0f}" height="{bh}" rx="3"/>')
            label_cls = "white" if cls != "f2" else "sub"
            body.append(f'<text class="{label_cls}" x="{x + 8:.0f}" y="{y + bh / 2 + 4:.0f}">{count} {word}</text>')
            x += w
    y = top + 3 * (bh + gap)
    body.append(f'<text class="axis" x="24" y="{y + 6}">Alerts: all 6 planted events found in tests; 0 false alerts over '
                f'351 simulated days.</text>')
    return svg(y + 24, "Accuracy of the built-in engine", body)


def main():
    now = dt.datetime(2026, 10, 21, 14, 40)
    bank = Bank(now)
    conn = bank.connect()
    with open("reports/eval_builtin_questions.json") as f:
        main_set = json.load(f)["summary"]
    with open("reports/eval_unseen_first_run.json") as f:
        unseen = json.load(f)
    os.makedirs("docs", exist_ok=True)
    for name, text in (("docs/flow.svg", flow()), ("docs/alert.svg", alert_chart(conn, now)),
                       ("docs/accuracy.svg", accuracy_chart(main_set, unseen))):
        with open(name, "w") as f:
            f.write(text)
        print("Wrote", name)


if __name__ == "__main__":
    main()
