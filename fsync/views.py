"""fsync view builders — the single source of truth for every UI surface.

Each function turns fsyncd API data (plain dicts from fsync.client) into a
dream4ui-lite Screen. The TUI renders these via ui_lite.render_textual; the
web process renders the SAME Screens via ui_lite.render_web. No rendering,
Rich, or HTML lives here — only the mapping from API state to view spec, so
the terminal and browser can never drift.
"""

from __future__ import annotations

import time

from dream4devops.ui_lite import (
    Action,
    Actions,
    Column,
    Note,
    Screen,
    Segment,
    Strip,
    Table,
    Tone,
)
from dream4devops.ui_lite.spec import cell, row

STATE_MARK = {"both": ("⇅", Tone.good), "push": ("→", Tone.accent),
              "pull": ("←", Tone.accent), "off": ("·", Tone.muted)}
PHASE_LABEL = {"queued": "queued", "indexing": "⣷ indexing…", "merging": "⇄ merging",
               "a_to_b": "→ pushing", "b_to_a": "← pulling", "done": "✓ done"}


def fmt_bytes(n) -> str:
    n = n or 0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return "?"


def fmt_clock(ts) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "?"


# --------------------------------------------------------------------------- #
# status strip — shared header on every screen                                #
# --------------------------------------------------------------------------- #

def status_strip(daemon_status: dict | None, peer_line: str | None = None) -> Strip:
    segs: list[Segment] = []
    if daemon_status is None:
        segs.append(Segment(text="✗ fsyncd unreachable", tone=Tone.bad, bold=True))
        return Strip(segments=segs)
    runner = daemon_status.get("runner")
    if runner:
        segs.append(Segment(
            text=f"● run {runner['run_id']} active (pid {runner['pid']}, {runner['elapsed']:.0f}s)",
            tone=Tone.good, bold=True))
    else:
        segs.append(Segment(text="○ no sync running", tone=Tone.muted))
    sched = daemon_status.get("schedule") or {}
    if not sched.get("enabled"):
        segs.append(Segment(text="schedule: off", tone=Tone.warn))
    else:
        segs.append(Segment(text=f"next {fmt_clock(sched.get('next_ts'))}"))
    last = daemon_status.get("last")
    if last:
        rid = last["run_id"]
        when = f"{rid[9:11]}:{rid[11:13]}" if len(rid) >= 13 else rid
        txt = (f"last {when} — {last['moved']} moved, {last['conflicts']} held"
               + (f", {last['errors']} ERROR" if last.get("errors") else "")
               + (" [dry]" if last.get("dry_run") else ""))
        segs.append(Segment(text=txt, tone=Tone.bad if last.get("errors") else Tone.default))
    else:
        segs.append(Segment(text="no completed runs yet"))
    if peer_line:
        segs.append(Segment(text=peer_line, tone=Tone.accent))
    return Strip(segments=segs)


def _with_strip(strip: Strip, screen: Screen) -> Screen:
    return Screen(title=screen.title, blocks=[strip, *screen.blocks])


# --------------------------------------------------------------------------- #
# planning (live) and plan preview                                            #
# --------------------------------------------------------------------------- #

def planning_screen(partial: dict, strip: Strip) -> Screen:
    cols = [Column(label="profile / path"), Column(label="state"),
            Column(label="→ push", align="right"), Column(label="← pull", align="right"),
            Column(label="conflicts", align="right"), Column(label="identical", align="right")]
    rows = []
    done = total = 0
    for pname, pdata in partial.get("profiles", {}).items():
        for rel, d in pdata.get("paths", {}).items():
            total += 1
            phase = d.get("phase", "queued")
            if phase == "done":
                done += 1
                push = f"{d['a_to_b']} ({fmt_bytes(d['bytes_a_to_b'])})" if d.get("a_to_b") else "-"
                pull = f"{d['b_to_a']} ({fmt_bytes(d['bytes_b_to_a'])})" if d.get("b_to_a") else "-"
                rows.append(row(cell(f"{pname}/{rel}"), cell("✓ planned", Tone.good),
                                cell(push), cell(pull),
                                cell(d.get("conflicts") or "-"), cell(d.get("identical", ""))))
            else:
                rows.append(row(cell(f"{pname}/{rel}"),
                                cell(PHASE_LABEL.get(phase, phase),
                                     Tone.warn if phase == "indexing" else Tone.muted),
                                cell(""), cell(""), cell(""), cell("")))
    table = Table(title="Planning… (both boxes hash locally; cached files are stat-only)",
                  columns=cols, rows=rows)
    note = Note(text=f"{done}/{total} paths planned — the preview with toggles appears "
                     "when all are in.   q: quit")
    return _with_strip(strip, Screen(blocks=[table, note]))


def plan_screen(plan: dict, state: dict, dry: bool, strip: Strip,
                msg: str = "") -> Screen:
    cols = [Column(label="on"), Column(label="profile / path"),
            Column(label="→ push", align="right"), Column(label="← pull", align="right"),
            Column(label="overwrite→backup", align="right"),
            Column(label="conflicts", align="right"), Column(label="identical", align="right")]
    rows = []
    totals = [0, 0, 0, 0]
    total_bytes = 0
    names = list(plan.get("profiles", {}))
    for i, pname in enumerate(names, start=1):
        pdata = plan["profiles"][pname]
        st = state.get(pname, "both")
        sym, sym_tone = STATE_MARK[st]
        take_push, take_pull = st in ("both", "push"), st in ("both", "pull")
        for j, (rel, d) in enumerate(pdata.get("paths", {}).items()):
            over = (d["overwrites_a_to_b"] if take_push else 0) + \
                   (d["overwrites_b_to_a"] if take_pull else 0)
            push = (f"{d['a_to_b']} ({fmt_bytes(d['bytes_a_to_b'])})" if d["a_to_b"] and take_push
                    else (f"{d['a_to_b']} skipped" if d["a_to_b"] else "-"))
            pull = (f"{d['b_to_a']} ({fmt_bytes(d['bytes_b_to_a'])})" if d["b_to_a"] and take_pull
                    else (f"{d['b_to_a']} skipped" if d["b_to_a"] else "-"))
            mark = f"{i} {sym}" if j == 0 else ""
            row_tone = Tone.muted if st == "off" else Tone.default
            strike = st == "off"
            active_cell = (d["a_to_b"] or d["b_to_a"] or d["conflicts"]) and st != "off"
            rows.append(row(
                cell(mark, sym_tone),
                cell(f"{pname}/{rel}", Tone.default if active_cell else Tone.muted, strike=strike),
                cell(push, Tone.muted if (not take_push and d["a_to_b"]) else Tone.default, strike=strike),
                cell(pull, Tone.muted if (not take_pull and d["b_to_a"]) else Tone.default, strike=strike),
                cell(over or "-", strike=strike),
                cell(d["conflicts"] or "-", Tone.warn if d["conflicts"] and st != "off" else Tone.default, strike=strike),
                cell(d["identical"], strike=strike), tone=row_tone))
            if take_push:
                totals[0] += d["a_to_b"]; total_bytes += d["bytes_a_to_b"]
            if take_pull:
                totals[1] += d["b_to_a"]; total_bytes += d["bytes_b_to_a"]
            if st != "off":
                totals[2] += over; totals[3] += d["conflicts"]
    active = sum(1 for s in state.values() if s != "off")
    rows.append(row(cell(""), cell(f"TOTAL ({active}/{len(names)} profiles active)"),
                    cell(totals[0]), cell(totals[1]), cell(totals[2]), cell(totals[3]),
                    cell(""), tone=Tone.accent, divider_before=True))
    table = Table(title=f"Plan preview -> {plan.get('peer', '')}"
                        f"{'   [DRY RUN]' if dry else ''}", columns=cols, rows=rows)
    blocks = [table]
    hint = (f"{fmt_bytes(total_bytes)} to move. Overwritten files are backed up on the receiver "
            "under ~/.fsync/backups/<run>/. Conflicts (review profiles) are held.")
    blocks.append(Note(text=hint))
    if msg:
        blocks.append(Note(text=msg, tone=Tone.warn))
    blocks.append(Actions(items=[
        Action(key="r", label="execute", tone=Tone.good),
        *[Action(key=str(i + 1), label=f"cycle {n}", tone=Tone.accent) for i, n in enumerate(names)],
        Action(key="d", label=f"dry-run [{'ON' if dry else 'off'}]"),
        Action(key="p", label="re-plan"),
        Action(key="q", label="quit"),
    ]))
    return _with_strip(strip, Screen(blocks=blocks))


# --------------------------------------------------------------------------- #
# run progress / finished                                                     #
# --------------------------------------------------------------------------- #

def progress_screen(progress: dict, mode: str, strip: Strip) -> Screen:
    status = progress.get("status", "?")
    elapsed = (progress.get("finished_ts") or time.time()) - (progress.get("started_ts") or time.time())
    title = {"progress": f"Sync running (pid {progress.get('pid')}) — {elapsed:.0f}s",
             "finished": f"Sync {status.upper()} in {elapsed:.0f}s",
             "error": f"Sync ERROR after {elapsed:.0f}s"}.get(mode, "Sync")
    if progress.get("dry_run"):
        title += "   [DRY RUN]"
    cols = [Column(label="profile / path"), Column(label="state"),
            Column(label="→ push", align="right"), Column(label="← pull", align="right"),
            Column(label="conflicts", align="right"), Column(label="time", align="right")]
    rows = []
    conflicts_total = 0
    for pname, pdata in progress.get("profiles", {}).items():
        paths = pdata.get("paths", {})
        if not paths and pdata.get("status") == "pending":
            rows.append(row(cell(pname), cell("pending", Tone.muted),
                            cell(""), cell(""), cell(""), cell(""), tone=Tone.muted))
            continue
        for rel, ps in paths.items():
            phase = ps.get("phase", "?")
            if phase == "done":
                ab, ba = ps.get("a_to_b", {}), ps.get("b_to_a", {})
                push = str(ab.get("files_transferred", 0)) + (
                    f" ({ab.get('overwrites_expected', 0)}⤺)" if ab.get("overwrites_expected") else "")
                pull = str(ba.get("files_transferred", 0)) + (
                    f" ({ba.get('overwrites_expected', 0)}⤺)" if ba.get("overwrites_expected") else "")
                cf = ps.get("conflicts", 0)
                conflicts_total += cf
                rows.append(row(cell(f"{pname}/{rel}"), cell("✓ done", Tone.good),
                                cell(push), cell(pull), cell(cf or "-"),
                                cell(f"{ps.get('seconds', 0)}s")))
            else:
                leg = ps.get("leg")
                detail = ""
                if leg:
                    detail = fmt_bytes(leg.get("bytes"))
                    if leg.get("pct") is not None:
                        detail += f" {leg['pct']}%"
                    if leg.get("files_done") is not None and leg.get("files_total"):
                        detail += f" ({leg['files_done']}/{leg['files_total']} files)"
                rows.append(row(cell(f"{pname}/{rel}"),
                                cell(PHASE_LABEL.get(phase, phase), Tone.warn),
                                cell(detail if (leg and leg.get("name") == "a_to_b") else ""),
                                cell(detail if (leg and leg.get("name") == "b_to_a") else ""),
                                cell(""), cell("")))
    table = Table(title=title, columns=cols, rows=rows)
    blocks = [table]
    for err in progress.get("errors", []):
        blocks.append(Note(text=f"ERROR: {err}", tone=Tone.bad))
    if mode == "progress":
        blocks.append(Note(text="Run continues even if you quit — reconnect any time."))
    else:
        if conflicts_total:
            blocks.append(Note(text=f"{conflicts_total} conflict(s) held for review", tone=Tone.warn))
        blocks.append(Note(text=f"report: {progress.get('run_dir', '')}/report.json"))
        blocks.append(Actions(items=[Action(key="p", label="plan a new run", tone=Tone.accent),
                                      Action(key="q", label="quit")]))
    return _with_strip(strip, Screen(blocks=blocks))


def message_screen(title: str, text: str, tone: Tone, strip: Strip,
                   actions: list[Action] | None = None) -> Screen:
    blocks = [Note(text=text, tone=tone)]
    if actions:
        blocks.append(Actions(items=actions))
    return _with_strip(strip, Screen(title=title, blocks=blocks))
