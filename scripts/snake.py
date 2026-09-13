#!/usr/bin/env python3
"""
Growing contribution snake for a GitHub profile README.

Fetches a user's contribution calendar, plays a game of snake on it
(greedy BFS to the nearest uneaten contribution square, +1 segment per
square eaten, reset when the board is clear) and writes the result as
pure-CSS-animated SVGs (snake-dark.svg / snake-light.svg) that GitHub can
display inside an <img>.

Usage:
    python scripts/snake.py --user nexsites --out dist
    python scripts/snake.py --user nexsites --out dist --token ghp_...   # GraphQL instead of HTML

Self-contained: standard library only, Python 3.8+.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Dict, List, Optional, Sequence, Set, Tuple

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

CELL = 12          # cell size in px
GAP = 3            # gap between cells in px
STEP = CELL + GAP  # grid pitch
MARGIN = 4         # outer margin in px
RX = 2             # cell corner radius
HEAD_RX = 3        # head corner radius
ROWS = 7           # Sunday..Saturday

START_LEN = 3      # snake length (head included) at the start of each loop
TICK = 0.1         # seconds per move
MAX_LOOP = 60.0    # if the loop would be longer than this, ticks are shortened
HOLD_TICKS = 12    # pause on the cleared board before the loop restarts

PALETTES = {
    "dark": {
        "empty": "#161b22",
        "levels": ["#0e4429", "#006d32", "#26a641", "#39d353"],
        "body": "#e0a030",
        "head": "#f5c15a",
    },
    "light": {
        "empty": "#ebedf0",
        "levels": ["#9be9a8", "#40c463", "#30a14e", "#216e39"],
        "body": "#d08a1e",
        "head": "#f0b640",
    },
}

Cell = Tuple[int, int]  # (column, row)


class Day:
    __slots__ = ("date", "level", "count")

    def __init__(self, date: dt.date, level: int, count: int):
        self.date = date
        self.level = level
        self.count = count

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Day({self.date}, level={self.level}, count={self.count})"


# --------------------------------------------------------------------------- #
# Data: HTML endpoint (default) or GraphQL (with --token)
# --------------------------------------------------------------------------- #

def _http(url: str, headers: Dict[str, str], data: Optional[bytes] = None, retries: int = 4) -> bytes:
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:  # noqa: PERF203
            last = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (400, 401, 403, 404):
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"request to {url} failed after {retries} attempts: {last}")


_TD_RE = re.compile(r"<td\b[^>]*\bdata-date=[\"'][^\"']*[\"'][^>]*>", re.I)
_ATTR_RE = re.compile(r"\b([a-zA-Z_:][-a-zA-Z0-9_:.]*)=([\"'])(.*?)\2")
_TIP_RE = re.compile(r"<tool-tip\b([^>]*)>(.*?)</tool-tip>", re.I | re.S)
_COUNT_RE = re.compile(r"(\d[\d,]*)\s+contributions?", re.I)


def _attrs(tag: str) -> Dict[str, str]:
    """Attribute name -> value for a tag body; single or double quotes accepted."""
    return {name: value for name, _quote, value in _ATTR_RE.findall(tag)}


def fetch_days_html(user: str) -> List[Day]:
    """Scrape https://github.com/users/<user>/contributions.

    This endpoint honours the user's "include private contributions" setting,
    exactly like the graph on their profile page.
    """
    url = f"https://github.com/users/{user}/contributions"
    raw = _http(url, {
        "User-Agent": "Mozilla/5.0 (compatible; contribution-snake/1.0; +https://github.com/)",
        "Accept": "text/html",
        "Accept-Language": "en-US,en;q=0.9",
    })
    text = raw.decode("utf-8", "replace")

    # Tooltip text keyed by the id of the <td> it describes ("3 contributions on May 24th.")
    tips: Dict[str, str] = {}
    for attrs, body in _TIP_RE.findall(text):
        a = _attrs(attrs)
        target = a.get("for")
        if target:
            tips[target] = html.unescape(re.sub(r"<[^>]+>", "", body)).strip()

    days: List[Day] = []
    for tag in _TD_RE.findall(text):
        a = _attrs(tag)
        date_s = a.get("data-date")
        if not date_s:
            continue
        try:
            date = dt.date.fromisoformat(date_s)
        except ValueError:
            continue
        try:
            level = int(a.get("data-level", "0"))
        except ValueError:
            level = 0
        level = max(0, min(4, level))
        count = 0
        tip = tips.get(a.get("id", ""), "")
        if tip and not tip.lower().startswith("no contribution"):
            m = _COUNT_RE.search(tip)  # "3 contributions on May 24th." - number anchored to the word
            if m:
                count = int(m.group(1).replace(",", ""))
        if count == 0 and level > 0:
            count = 1  # tooltip missing/unparseable; the level says there was activity
        days.append(Day(date, level, count))

    if not days:
        raise RuntimeError("no contribution cells found in the HTML; GitHub may have changed its markup")
    return days


_GQL_LEVELS = {"NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2, "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4}


def fetch_days_graphql(user: str, token: str) -> List[Day]:
    query = """
    query($login: String!) {
      user(login: $login) {
        contributionsCollection {
          contributionCalendar {
            weeks { contributionDays { date contributionCount contributionLevel } }
          }
        }
      }
    }"""
    payload = json.dumps({"query": query, "variables": {"login": user}}).encode()
    raw = _http("https://api.github.com/graphql", {
        "Authorization": f"bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "contribution-snake/1.0",
    }, data=payload)
    doc = json.loads(raw)
    if doc.get("errors"):
        raise RuntimeError("GraphQL error: " + "; ".join(e.get("message", "?") for e in doc["errors"]))
    user_node = (doc.get("data") or {}).get("user")
    if not user_node:
        raise RuntimeError(f"GraphQL returned no user for {user!r}")
    days: List[Day] = []
    for week in user_node["contributionsCollection"]["contributionCalendar"]["weeks"]:
        for d in week["contributionDays"]:
            days.append(Day(dt.date.fromisoformat(d["date"]),
                            _GQL_LEVELS.get(d["contributionLevel"], 0),
                            int(d["contributionCount"])))
    if not days:
        raise RuntimeError("GraphQL returned an empty calendar")
    return days


# --------------------------------------------------------------------------- #
# Grid layout: weeks as columns, Sunday first (like GitHub)
# --------------------------------------------------------------------------- #

def layout(days: Sequence[Day]) -> Tuple[int, Dict[Cell, Day]]:
    """Return (number_of_columns, {(col, row): Day})."""
    days = sorted(days, key=lambda d: d.date)
    first = days[0].date
    first_sunday = first - dt.timedelta(days=(first.weekday() + 1) % 7)
    grid: Dict[Cell, Day] = {}
    for d in days:
        col = (d.date - first_sunday).days // 7
        row = (d.date.weekday() + 1) % 7  # Sunday=0 ... Saturday=6
        grid[(col, row)] = d
    width = max(c for c, _ in grid) + 1
    return width, grid


# --------------------------------------------------------------------------- #
# Simulation: greedy BFS snake
# --------------------------------------------------------------------------- #

def bfs(start: Cell, cells: Set[Cell]) -> Tuple[Dict[Cell, int], Dict[Cell, Cell]]:
    """Shortest paths from `start` over the calendar cells only (no walking through
    the empty slots of the leading/trailing partial weeks, which have no <rect>)."""
    dist: Dict[Cell, int] = {start: 0}
    parent: Dict[Cell, Cell] = {}
    q: deque = deque([start])
    while q:
        c, r = q.popleft()
        for dc, dr in ((1, 0), (0, 1), (0, -1), (-1, 0)):
            n = (c + dc, r + dr)
            if n in cells and n not in dist:
                dist[n] = dist[(c, r)] + 1
                parent[n] = (c, r)
                q.append(n)
    return dist, parent


def edge_cell(cells: Set[Cell], col: int) -> Cell:
    """The existing cell in `col` closest to the middle row (ties -> upper row)."""
    return min((c for c in cells if c[0] == col), key=lambda c: (abs(c[1] - ROWS // 2), c[1]))


def simulate(cells: Set[Cell], food: Set[Cell], start: Cell) -> Tuple[List[Cell], Dict[Cell, int]]:
    """Return (head positions per tick, {cell: tick when it was eaten})."""
    path: List[Cell] = [start]
    eaten: Dict[Cell, int] = {}
    remaining = set(food)
    if start in remaining:
        remaining.discard(start)
        eaten[start] = 0
    head = start
    while remaining:
        dist, parent = bfs(head, cells)
        # nearest first; ties -> leftmost column, then top row
        target = min(remaining, key=lambda p: (dist[p], p[0], p[1]))
        segment: List[Cell] = []
        cur = target
        while cur != head:
            segment.append(cur)
            cur = parent[cur]
        segment.reverse()
        for p in segment:
            path.append(p)
            if p in remaining:  # anything run over on the way gets eaten too
                remaining.discard(p)
                eaten[p] = len(path) - 1
        head = target
    return path, eaten


# --------------------------------------------------------------------------- #
# Rendering: pure CSS keyframes
# --------------------------------------------------------------------------- #

BASE, SNAKE, EATEN = 0, 1, 2


def fmt(value: float) -> str:
    """Number rounded to 3 decimals with trailing zeros stripped ("12.5", "0")."""
    s = f"{value:.3f}".rstrip("0").rstrip(".")
    return s if s else "0"


def cell_states(path: List[Cell], eaten: Dict[Cell, int], width: int, height: int) -> Dict[Cell, List[int]]:
    """Per cell, the state at every tick of the moving phase (BASE/SNAKE/EATEN)."""
    ticks = len(path)
    eat_ticks = sorted(eaten.values())
    states: Dict[Cell, List[int]] = {}
    for c in range(width):
        for r in range(height):
            cell = (c, r)
            if cell in eaten:
                e = eaten[cell]
                states[cell] = [BASE] * e + [EATEN] * (ticks - e)
            else:
                states[cell] = [BASE] * ticks
    eaten_so_far = 0
    for t in range(ticks):
        while eaten_so_far < len(eat_ticks) and eat_ticks[eaten_so_far] <= t:
            eaten_so_far += 1
        length = START_LEN + eaten_so_far
        for k in range(1, length):
            i = t - k
            if i < 0:
                break  # the initial body is off-grid to the left of the board
            states[path[i]][t] = SNAKE
    return states


def runs(seq: List[int]) -> List[Tuple[int, int]]:
    """Compress a per-tick state list into [(start_tick, state), ...]."""
    out: List[Tuple[int, int]] = []
    for t, s in enumerate(seq):
        if not out or out[-1][1] != s:
            out.append((t, s))
    return out


def build_svg(width: int, grid: Dict[Cell, Day], path: List[Cell], eaten: Dict[Cell, int],
              palette: Dict[str, object], states: Dict[Cell, List[int]], tick: float) -> str:
    height = ROWS
    move_ticks = len(path)
    total_ticks = move_ticks + HOLD_TICKS
    duration = total_ticks * tick
    dur_s = fmt(duration) + "s"

    empty = str(palette["empty"])
    levels = list(palette["levels"])  # type: ignore[arg-type]
    body = str(palette["body"])
    head = str(palette["head"])

    def base_color(cell: Cell) -> str:
        d = grid.get(cell)
        if d is None or d.level <= 0:
            return empty
        return levels[min(4, d.level) - 1]

    def pct(t: int) -> str:
        return fmt(100.0 * t / total_ticks)

    def cell_xy(cell: Cell) -> Tuple[int, int]:
        return MARGIN + cell[0] * STEP, MARGIN + cell[1] * STEP

    # keyframes per cell, deduplicated by their body
    keyframes: Dict[str, str] = {}       # body -> name
    cell_anim: Dict[Cell, str] = {}
    for cell, seq in states.items():
        rs = runs(seq)
        if len(rs) == 1 and rs[0][1] == BASE:
            continue  # never touched: static rect
        colors = {BASE: base_color(cell), SNAKE: body, EATEN: empty}
        stops = [f"{pct(t)}%{{fill:{colors[s]}}}" for t, s in rs]
        last_color = colors[rs[-1][1]]
        stops.append(f"100%{{fill:{last_color}}}")
        kf_body = "".join(stops)
        name = keyframes.get(kf_body)
        if name is None:
            name = f"k{len(keyframes)}"
            keyframes[kf_body] = name
        cell_anim[cell] = name

    # head transform keyframes: one stop per tick, held during the pause
    head_stops = []
    for t, cell in enumerate(path):
        x, y = cell_xy(cell)
        head_stops.append(f"{pct(t)}%{{transform:translate({x}px,{y}px)}}")
    hx, hy = cell_xy(path[-1])
    head_stops.append(f"100%{{transform:translate({hx}px,{hy}px)}}")

    svg_w = MARGIN * 2 + width * STEP - GAP
    svg_h = MARGIN * 2 + height * STEP - GAP

    css = [
        f".c{{animation-duration:{dur_s};animation-timing-function:step-end;animation-iteration-count:infinite;animation-fill-mode:both}}",
        f".l0{{fill:{empty}}}",
    ]
    for i, col in enumerate(levels, start=1):
        css.append(f".l{i}{{fill:{col}}}")
    for kf_body, name in keyframes.items():
        css.append(f".{name}{{animation-name:{name}}}")
        css.append(f"@keyframes {name}{{{kf_body}}}")
    css.append(f".h{{fill:{head};animation:hd {dur_s} step-end infinite}}")
    css.append("@keyframes hd{" + "".join(head_stops) + "}")

    rects = []
    for r in range(height):
        for c in range(width):
            cell = (c, r)
            if cell not in grid:
                continue  # days outside the calendar (leading/trailing partial weeks)
            x, y = cell_xy(cell)
            lvl = max(0, min(4, grid[cell].level))
            cls = f"l{lvl}"
            anim = cell_anim.get(cell)
            if anim:
                cls += f" c {anim}"
            rects.append(f'<rect class="{cls}" x="{x}" y="{y}" width="{CELL}" height="{CELL}" rx="{RX}"/>')
    rects.append(f'<rect class="h" x="0" y="0" width="{CELL}" height="{CELL}" rx="{HEAD_RX}"/>')

    total_contrib = sum(d.count for d in grid.values())
    title = f"Contribution snake: {len(eaten)} squares, {total_contrib} contributions, {move_ticks} moves"

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_w}" height="{svg_h}" '
        f'viewBox="0 0 {svg_w} {svg_h}" role="img" aria-label="{html.escape(title)}">'
        f"<title>{html.escape(title)}</title>"
        "<style>" + "".join(css) + "</style>"
        + "".join(rects)
        + "</svg>\n"
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Render a growing contribution snake as animated SVGs.")
    ap.add_argument("--user", required=True, help="GitHub login")
    ap.add_argument("--out", default="dist", help="output directory (default: dist)")
    ap.add_argument("--token", default=None,
                    help="GitHub token; when set the GraphQL API is used instead of the public HTML endpoint")
    ap.add_argument("--tick", type=float, default=TICK, help=f"seconds per move (default {TICK})")
    ap.add_argument("--max-loop", type=float, default=MAX_LOOP, help=f"max loop length in seconds (default {MAX_LOOP})")
    args = ap.parse_args(argv)

    token = args.token or None
    if token:
        days = fetch_days_graphql(args.user, token)
        source = "graphql"
    else:
        days = fetch_days_html(args.user)
        source = "html"

    width, grid = layout(days)
    cells = set(grid)
    food = {cell for cell, d in grid.items() if d.level > 0}
    # start on an existing cell at the left edge (the first week may be partial)
    start = edge_cell(cells, 0)
    if food:
        path, eaten = simulate(cells, food, start)
    else:
        # nothing to eat: still produce a valid (if dull) animation by walking to the
        # right edge - without pretending that the destination was a contribution
        path, _ = simulate(cells, {edge_cell(cells, width - 1)}, start)
        eaten = {}

    tick = args.tick
    total_ticks = len(path) + HOLD_TICKS
    if total_ticks * tick > args.max_loop:
        tick = args.max_loop / total_ticks

    states = cell_states(path, eaten, width, ROWS)

    os.makedirs(args.out, exist_ok=True)
    sizes = {}
    for name, palette in PALETTES.items():
        svg = build_svg(width, grid, path, eaten, palette, states, tick)
        out_path = os.path.join(args.out, f"snake-{name}.svg")
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(svg)
        sizes[name] = len(svg.encode("utf-8"))

    print(f"user={args.user} source={source} days={len(days)} columns={width} "
          f"food={len(food)} moves={len(path)} tick={tick:.4f}s loop={total_ticks * tick:.2f}s "
          f"dark={sizes['dark']}B light={sizes['light']}B -> {os.path.abspath(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
