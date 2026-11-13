#!/usr/bin/env python3
"""Render the README telemetry card as a self-contained SVG.

Runs daily in .github/workflows/profile-assets.yml. Pulls public activity from
the GitHub GraphQL API and draws it in the same HUD style as
assets/hero-banner.svg, so the profile doesn't depend on third-party stat
services staying online. Standard library only.

  GITHUB_TOKEN=... python3 profile_card.py --user aaziy --out dist/profile-card.svg
  python3 profile_card.py --from-json data.json --out card.svg     # offline render
  GITHUB_TOKEN=... python3 profile_card.py --user aaziy --dump-json data.json
"""

import argparse
import datetime as dt
import json
import math
import os
import sys
import urllib.request
from collections import Counter
from xml.sax.saxutils import escape

# ── configuration ────────────────────────────────────────────────────────────
# Markup / notebook output inflates byte counts without saying much about the
# code someone writes, so it's left out of the language breakdown.
HIDDEN_LANGS = {"HTML", "CSS", "SCSS", "Less", "Jupyter Notebook", "Procfile"}
TOP_LANGS = 6

# Validated categorical palette (dark surface #0B0E14: lightness band, chroma,
# CVD separation, contrast all pass). Known languages keep a fixed color so a
# shift in ranking never repaints them; anything else takes the next free slot.
PALETTE = ["#1597B3", "#C98500", "#9085E9", "#D95926", "#3987E5", "#D55181"]
FIXED_COLORS = {"Python": 0, "TypeScript": 1, "JavaScript": 2, "C++": 3, "Java": 4}
OTHER_COLOR = "#3A4654"

# Tokens shared with the hero banner.
BG_INNER, BG_OUTER = "#121822", "#0B0E14"
GRID, BORDER = "#16202B", "#1B2330"
PANEL, PANEL_STROKE = "#10151D", "#1A5A68"
CYAN, AMBER = "#2EE6FF", "#FFC857"
INK, INK_2, INK_MUTED = "#E6EDF3", "#C7D4DD", "#7F93A3"
FONT = "'JetBrains Mono','SFMono-Regular',ui-monospace,Menlo,Consolas,'DejaVu Sans Mono',monospace"

W = 840
PAD = 32

# ── data ─────────────────────────────────────────────────────────────────────
QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    login
    followers { totalCount }
    allRepos: repositories(ownerAffiliations: OWNER, privacy: PUBLIC) { totalCount }
    repositories(first: 100, after: $cursor, ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC) {
      pageInfo { hasNextPage endCursor }
      nodes {
        stargazerCount
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name } }
        }
      }
    }
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


def graphql(query, variables, token):
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-card-script",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


def fetch(login, token):
    """Return the normalized data dict the renderer consumes."""
    cursor, repos, first = None, [], None
    while True:
        data = graphql(QUERY, {"login": login, "cursor": cursor}, token)["user"]
        if data is None:
            raise RuntimeError(f"user {login!r} not found")
        first = first or data
        page = data["repositories"]
        repos.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    languages = {}
    for repo in repos:
        for edge in repo["languages"]["edges"]:
            entry = languages.setdefault(edge["node"]["name"], {"bytes": 0, "repos": 0})
            entry["bytes"] += edge["size"]
            entry["repos"] += 1

    calendar = first["contributionsCollection"]["contributionCalendar"]
    days = [
        [d["date"], d["contributionCount"]]
        for week in calendar["weeks"]
        for d in week["contributionDays"]
    ]
    return {
        "login": first["login"],
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "public_repos": first["allRepos"]["totalCount"],
        "followers": first["followers"]["totalCount"],
        "stars": sum(r["stargazerCount"] for r in repos),
        "days": days,
        "languages": languages,
    }


# ── derived metrics ──────────────────────────────────────────────────────────
def summarize(data):
    days = sorted((dt.date.fromisoformat(d), c) for d, c in data["days"])
    total = sum(c for _, c in days)
    active = sum(1 for _, c in days if c > 0)

    longest = run = 0
    prev = None
    for day, count in days:
        contiguous = prev is not None and (day - prev).days == 1
        run = (run + 1 if contiguous else 1) if count > 0 else 0
        longest = max(longest, run)
        prev = day

    by_weekday = Counter()
    for day, count in days:
        by_weekday[day.strftime("%a").upper()] += count
    busiest_day = by_weekday.most_common(1)[0][0] if total else "—"

    # GitHub calendars start on a Sunday; bucket into those weeks, keep the last 52.
    start = days[0][0] - dt.timedelta(days=(days[0][0].weekday() + 1) % 7)
    weeks = {}
    for day, count in days:
        idx = (day - start).days // 7
        weeks.setdefault(idx, [start + dt.timedelta(weeks=idx), 0])[1] += count
    weekly = [weeks[i] for i in sorted(weeks)][-52:]

    langs = {k: v for k, v in data["languages"].items() if k not in HIDDEN_LANGS}
    # Weighted like github-readme-stats' size_weight=0.5,count_weight=0.5 so one
    # huge repo can't drown out a language used across many projects.
    scores = {k: math.sqrt(v["bytes"]) * math.sqrt(v["repos"]) for k, v in langs.items()}
    score_total = sum(scores.values()) or 1
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top = [(name, s / score_total) for name, s in ranked[:TOP_LANGS]]
    rest = sum(s for _, s in ranked[TOP_LANGS:]) / score_total

    return {
        "total": total,
        "active": active,
        "longest": longest,
        "busiest_day": busiest_day,
        "weekly": weekly,
        "repos": data["public_repos"],
        "lang_count": len(langs),
        "top_langs": top,
        "other_share": rest,
    }


def lang_colors(names):
    used = {FIXED_COLORS[n] for n in names if n in FIXED_COLORS}
    free = [i for i in range(len(PALETTE)) if i not in used]
    colors = {}
    for name in names:
        if name in FIXED_COLORS:
            colors[name] = PALETTE[FIXED_COLORS[name]]
        else:
            colors[name] = PALETTE[free.pop(0)] if free else OTHER_COLOR
    return colors


# ── svg helpers ──────────────────────────────────────────────────────────────
def text(x, y, s, size, fill, weight=400, anchor="start", spacing=0, extra=""):
    ls = f' letter-spacing="{spacing}"' if spacing else ""
    an = f' text-anchor="{anchor}"' if anchor != "start" else ""
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}"{an}{ls}{extra}>{escape(str(s))}</text>'
    )


def nice_ceiling(v):
    if v <= 4:
        return 4
    mag = 10 ** math.floor(math.log10(v))
    for step in (1, 2, 2.5, 5, 10):
        if v <= step * mag:
            return int(step * mag) if step * mag >= 4 else 4
    return int(10 * mag)


# ── card ─────────────────────────────────────────────────────────────────────
def render(data):
    m = summarize(data)
    synced = dt.datetime.strptime(data["generated_at"], "%Y-%m-%dT%H:%M:%SZ")
    out = []
    H = 438

    # backdrop + frame (matches the banner)
    out.append(f'<rect width="{W}" height="{H}" rx="14" fill="url(#bg)"/>')
    out.append(f'<rect width="{W}" height="{H}" rx="14" fill="url(#grid)" opacity="0.5"/>')
    out.append(f'<rect x="0.75" y="0.75" width="{W-1.5}" height="{H-1.5}" rx="13.25" fill="none" stroke="{BORDER}" stroke-width="1.5"/>')
    a, i = 18, 14
    out.append(
        f'<g stroke="{CYAN}" stroke-width="2.5" fill="none" stroke-linecap="round" opacity="0.85">'
        f'<path d="M{i},{i+a} L{i},{i} L{i+a},{i}"/>'
        f'<path d="M{W-i-a},{i} L{W-i},{i} L{W-i},{i+a}"/>'
        f'<path d="M{W-i},{H-i-a} L{W-i},{H-i} L{W-i-a},{H-i}"/>'
        f'<path d="M{i+a},{H-i} L{i},{H-i} L{i},{H-i-a}"/>'
        '<animate attributeName="opacity" values="0.5;1;0.5" dur="3.2s" repeatCount="indefinite"/></g>'
    )

    # header
    out.append(f'<rect x="{PAD}" y="33" width="4" height="14" rx="1" fill="{CYAN}"/>')
    out.append(text(PAD + 14, 45, "GITHUB TELEMETRY", 13, CYAN, 700, spacing=2))
    out.append(text(PAD + 14 + 16 * 9.8 + 8, 45, f"// @{data['login']}", 12, INK_MUTED))
    stamp = synced.strftime("SYNCED %d %b %Y · %H:%M UTC").upper()
    out.append(text(W - PAD, 45, stamp, 11, INK_MUTED, anchor="end", spacing=0.5))
    dot_x = W - PAD - len(stamp) * 7.1 - 12
    out.append(
        f'<circle cx="{dot_x:.1f}" cy="41" r="3.5" fill="{AMBER}">'
        '<animate attributeName="opacity" values="1;0.25;1" dur="1.7s" repeatCount="indefinite"/></circle>'
    )
    out.append(f'<line x1="{PAD}" y1="62" x2="{W-PAD}" y2="62" stroke="{GRID}" stroke-width="1"/>')

    # stat tiles
    tiles = [
        ("CONTRIBUTIONS", f"{m['total']:,}", "1y"),
        ("ACTIVE DAYS", f"{m['active']}", "1y"),
        ("LONGEST STREAK", f"{m['longest']}", "days"),
        ("BUSIEST DAY", m["busiest_day"], ""),
        ("PUBLIC REPOS", f"{m['repos']}", ""),
        ("LANGUAGES", f"{m['lang_count']}", ""),
    ]
    gap, ty, th = 10, 78, 68
    tw = (W - 2 * PAD - gap * (len(tiles) - 1)) / len(tiles)
    for n, (label, value, unit) in enumerate(tiles):
        x = PAD + n * (tw + gap)
        out.append(f'<rect x="{x:.1f}" y="{ty}" width="{tw:.1f}" height="{th}" rx="6" fill="{PANEL}" stroke="{PANEL_STROKE}" stroke-opacity="0.7"/>')
        out.append(text(x + 12, ty + 22, label, 9.5, INK_MUTED, 600, spacing=0.8))
        out.append(text(x + 12, ty + 54, value, 24, INK, 700))
        if unit:
            out.append(text(x + 12 + len(value) * 14.6 + 6, ty + 54, unit, 11, CYAN, 600))

    # weekly contributions — single-series area, so the title names it (no legend)
    weekly = m["weekly"]
    cy0 = 172
    out.append(text(PAD, cy0, "▸ CONTRIBUTIONS PER WEEK", 10.5, INK_2, 600, spacing=1.2))
    out.append(text(W - PAD, cy0, f"LAST {len(weekly)} WEEKS", 10, INK_MUTED, anchor="end", spacing=1))
    x0, x1 = PAD + 30, W - PAD - 10
    top, base = cy0 + 20, cy0 + 102
    peak = max((c for _, c in weekly), default=0)
    ymax = nice_ceiling(peak)
    for frac in (0.5, 1.0):
        gy = base - (base - top) * frac
        out.append(f'<line x1="{x0}" y1="{gy:.1f}" x2="{x1}" y2="{gy:.1f}" stroke="{GRID}" stroke-width="1" stroke-dasharray="2 4"/>')
        out.append(text(x0 - 8, gy + 3.5, f"{ymax * frac:g}", 9.5, INK_MUTED, anchor="end"))
    out.append(text(x0 - 8, base + 3.5, "0", 9.5, INK_MUTED, anchor="end"))

    step = (x1 - x0) / max(len(weekly) - 1, 1)
    pts = [(x0 + k * step, base - (base - top) * (c / ymax if ymax else 0)) for k, (_, c) in enumerate(weekly)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    length = sum(math.dist(pts[k], pts[k + 1]) for k in range(len(pts) - 1))
    area = f"M{pts[0][0]:.1f},{base} L" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + f" L{pts[-1][0]:.1f},{base} Z"
    out.append(
        f'<path d="{area}" fill="url(#area)">'
        '<animate attributeName="opacity" values="0;0;1" keyTimes="0;0.4;1" dur="2.4s" fill="freeze"/></path>'
    )
    out.append(f'<line x1="{x0}" y1="{base}" x2="{x1}" y2="{base}" stroke="{BORDER}" stroke-width="1"/>')
    out.append(
        f'<polyline points="{line}" fill="none" stroke="{CYAN}" stroke-width="2" stroke-linejoin="round" '
        f'stroke-linecap="round" stroke-dasharray="{length:.0f}" stroke-dashoffset="0">'
        f'<animate attributeName="stroke-dashoffset" values="{length:.0f};0" dur="2.4s" '
        'calcMode="spline" keySplines="0.4 0 0.2 1" fill="freeze"/></polyline>'
    )

    # month ticks
    last_label_x = -99
    for k, (wk_start, _) in enumerate(weekly):
        prev_month = weekly[k - 1][0].month if k else None
        if k and wk_start.month != prev_month and pts[k][0] - last_label_x > 34:
            out.append(text(pts[k][0], base + 16, wk_start.strftime("%b").upper(), 9.5, INK_MUTED, anchor="middle", spacing=0.5))
            last_label_x = pts[k][0]

    # peak: direct label, amber marker with a surface ring
    if peak:
        k = max(range(len(weekly)), key=lambda j: weekly[j][1])
        px, py = pts[k]
        out.append(f'<line x1="{px:.1f}" y1="{py:.1f}" x2="{px:.1f}" y2="{base}" stroke="{AMBER}" stroke-opacity="0.35" stroke-dasharray="2 3"/>')
        out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4.5" fill="{AMBER}" stroke="{BG_OUTER}" stroke-width="2"/>')
        label = f"PEAK {peak} · WK OF {weekly[k][0].strftime('%d %b').upper()}"
        lx = min(max(px, x0 + len(label) * 3.2), x1 - len(label) * 3.2)
        out.append(text(lx, py - 10, label, 9.5, INK_2, 600, anchor="middle", spacing=0.5))

    # live marker on the current week
    lx_, ly_ = pts[-1]
    out.append(
        f'<circle cx="{lx_:.1f}" cy="{ly_:.1f}" r="3.5" fill="{CYAN}"/>'
        f'<circle cx="{lx_:.1f}" cy="{ly_:.1f}" r="3.5" fill="none" stroke="{CYAN}" stroke-width="1.5">'
        '<animate attributeName="r" values="3.5;10" dur="2s" repeatCount="indefinite"/>'
        '<animate attributeName="opacity" values="0.8;0" dur="2s" repeatCount="indefinite"/></circle>'
    )

    # languages — segmented bar (2px surface gaps) + legend; text stays in ink
    ly0 = 322
    out.append(text(PAD, ly0, "▸ LANGUAGES", 10.5, INK_2, 600, spacing=1.2))
    out.append(text(W - PAD, ly0, "WEIGHTED BY CODE VOLUME × REPO COUNT", 10, INK_MUTED, anchor="end", spacing=1))
    segments = list(m["top_langs"])
    colors = lang_colors([n for n, _ in segments])
    if m["other_share"] > 0.0005:
        segments.append(("Other", m["other_share"]))
        colors["Other"] = OTHER_COLOR
    bx, by, bw, bh, seg_gap = PAD, ly0 + 14, W - 2 * PAD, 10, 2
    usable = bw - seg_gap * (len(segments) - 1)
    # one left-to-right reveal of the whole bar, so any mid-animation frame still reads as a bar
    out.append(
        f'<clipPath id="barclip"><rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="4">'
        f'<animate attributeName="width" values="0;{bw}" dur="1.6s" calcMode="spline" keySplines="0.4 0 0.2 1" fill="freeze"/>'
        f'</rect></clipPath><g clip-path="url(#barclip)">'
    )
    cursor = bx
    for name, share in segments:
        w = max(usable * share, 1)
        out.append(f'<rect x="{cursor:.1f}" y="{by}" width="{w:.1f}" height="{bh}" fill="{colors[name]}"/>')
        cursor += w + seg_gap
    out.append("</g>")

    cols, col_w = 4, (W - 2 * PAD) / 4
    for n, (name, share) in enumerate(segments):
        cx = PAD + (n % cols) * col_w
        cy = by + 36 + (n // cols) * 22
        out.append(f'<circle cx="{cx + 5:.1f}" cy="{cy - 4}" r="4.5" fill="{colors[name]}"/>')
        out.append(text(cx + 16, cy, name, 11.5, INK_2, 500))
        out.append(text(cx + 16 + len(name) * 6.9 + 8, cy, f"{share * 100:.1f}%", 11.5, INK_MUTED))

    defs = (
        "<defs>"
        f'<radialGradient id="bg" cx="50%" cy="30%" r="85%"><stop offset="0%" stop-color="{BG_INNER}"/><stop offset="100%" stop-color="{BG_OUTER}"/></radialGradient>'
        f'<linearGradient id="area" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="{CYAN}" stop-opacity="0.28"/><stop offset="100%" stop-color="{CYAN}" stop-opacity="0"/></linearGradient>'
        f'<pattern id="grid" width="28" height="28" patternUnits="userSpaceOnUse"><path d="M28 0 L0 0 0 28" fill="none" stroke="{GRID}" stroke-width="1"/></pattern>'
        "</defs>"
    )
    title = (
        f"GitHub telemetry for @{data['login']}: {m['total']} contributions and {m['active']} active days "
        f"in the last year, longest streak {m['longest']} days, {m['repos']} public repositories. "
        "Top languages: " + ", ".join(f"{n} {s * 100:.0f}%" for n, s in m["top_langs"]) + "."
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'role="img" aria-label="{escape(title)}" font-family="{FONT}">'
        f"<title>{escape(title)}</title>{defs}{''.join(out)}</svg>\n"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", default=os.environ.get("GITHUB_REPOSITORY_OWNER"))
    ap.add_argument("--out", help="where to write the SVG")
    ap.add_argument("--from-json", help="render from a saved data file instead of the API")
    ap.add_argument("--dump-json", help="also save the fetched data to this path")
    args = ap.parse_args()

    if args.from_json:
        with open(args.from_json) as f:
            data = json.load(f)
    else:
        token = os.environ.get("GITHUB_TOKEN")
        if not token or not args.user:
            sys.exit("need GITHUB_TOKEN and --user (or --from-json)")
        data = fetch(args.user, token)

    if args.dump_json:
        with open(args.dump_json, "w") as f:
            json.dump(data, f, indent=1)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as f:
            f.write(render(data))
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
