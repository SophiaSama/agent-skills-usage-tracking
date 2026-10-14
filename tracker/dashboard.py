from __future__ import annotations

from tracker import report


def _range_data(summary):
    skills = []
    unmatched = []
    for row in summary.rows:
        if row.copies:
            skills.append(
                {
                    "name": row.name,
                    "copyCount": len(row.copies),
                    "sources": sorted({copy.source for copy in row.copies}),
                    "counts": dict(row.counts),
                    "total": row.total,
                    "sessions": len(row.sessions),
                    "last": row.last.isoformat() if row.last else None,
                    "observed": row.total > 0,
                }
            )
        elif row.total:
            unmatched.append(
                {
                    "name": row.name,
                    "loads": row.total,
                    "sources": [source for source in report.SOURCES if row.counts[source]],
                    "last": row.last.isoformat() if row.last else None,
                }
            )

    observed = sum(skill["observed"] for skill in skills)
    return {
        "summary": {
            "installedSkills": len(skills),
            "recordedLoads": sum(row.total for row in summary.rows),
            "observedSkills": observed,
            "notObservedSkills": len(skills) - observed,
        },
        "skills": skills,
        "unmatched": unmatched,
    }


def build_dashboard_data(events, copies, now):
    recent = report.summarize(events, copies, now, 30)
    history = report.summarize_all(events, copies, now)
    return {
        "ranges": {
            "30d": _range_data(recent),
            "all": _range_data(history),
        }
    }


_HTML_PREFIX = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Copilot Skill Usage Dashboard</title>
<style>
:root{color-scheme:light;--ink:#1c2434;--muted:#647087;--line:#dce2eb;--paper:#fff;--wash:#f4f6fa;--blue:#3268c8;--green:#3c9275;--gold:#d7952b;--shadow:0 14px 40px #24354d12}
*{box-sizing:border-box}body{margin:0;background:var(--wash);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1240px;margin:0 auto;padding:42px 28px 64px}.eyebrow{text-transform:uppercase;letter-spacing:.14em;font-size:12px;font-weight:750;color:var(--blue)}
h1{margin:6px 0 4px;font-size:clamp(28px,4vw,42px);letter-spacing:-.04em}.lede{margin:0;color:var(--muted)}
.toolbar{display:flex;flex-wrap:wrap;gap:18px;align-items:center;justify-content:space-between;margin:28px 0 18px}.control-group{display:flex;gap:6px;align-items:center;flex-wrap:wrap}.control-label{font-size:12px;font-weight:700;color:var(--muted);margin-right:5px}
button{font:inherit;color:var(--ink);background:var(--paper);border:1px solid var(--line);border-radius:9px;padding:8px 12px;cursor:pointer}button:hover{border-color:var(--blue)}button[aria-pressed="true"]{background:#eaf0fc;border-color:#9bb5e7;color:#174b9f;font-weight:700}button:focus-visible{outline:3px solid #7aa5ef;outline-offset:2px}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:18px 0 24px}.card,.panel{background:var(--paper);border:1px solid var(--line);border-radius:15px;box-shadow:var(--shadow)}.card{padding:18px 20px}.card span{display:block;color:var(--muted);font-size:13px}.card strong{display:block;margin-top:3px;font-size:29px;letter-spacing:-.03em}
.panel{padding:22px;margin:16px 0}.panel h2{font-size:19px;margin:0 0 4px;letter-spacing:-.02em}.panel-intro{margin:0 0 16px;color:var(--muted);font-size:13px}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:13px 0 17px;color:var(--muted);font-size:12px}.legend span{display:inline-flex;align-items:center;gap:6px}.swatch{width:10px;height:10px;border-radius:3px;display:inline-block}.agent-read{background:var(--blue)}.user-slash{background:var(--green)}.skill-tool{background:var(--gold)}
.chart{display:grid;gap:11px}.chart-row{display:grid;grid-template-columns:minmax(120px,220px) 1fr 74px;gap:12px;align-items:center}.chart-name{overflow-wrap:anywhere;font-size:13px;font-weight:650}.bar-track{height:15px;background:#edf0f5;border-radius:99px;display:flex;overflow:hidden}.bar-segment{height:100%;min-width:0}.bar-total{text-align:right;color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}.zero-label{color:var(--muted);font-size:12px}
.table-scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;min-width:900px;font-size:13px}th,td{text-align:left;padding:11px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);white-space:nowrap}td.num{font-variant-numeric:tabular-nums;text-align:right}.status{display:inline-block;padding:2px 8px;border-radius:999px;background:#edf1f7;font-size:11px;font-weight:700;white-space:nowrap}.status.observed{background:#e6f4ef;color:#246b55}.source-tags{color:var(--muted);overflow-wrap:anywhere}.empty{padding:12px 14px;background:#f2f5fa;border-radius:9px;color:var(--muted);font-size:13px}.note{color:var(--muted);font-size:13px}.unmatched-table{min-width:580px}.footer{color:var(--muted);font-size:12px;margin-top:22px}
@media(max-width:760px){main{padding:28px 16px 44px}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.chart-row{grid-template-columns:minmax(90px,150px) 1fr 58px;gap:8px}.panel{padding:17px}}
@media(max-width:420px){.cards{gap:8px}.card{padding:13px}.card strong{font-size:24px}.chart-row{grid-template-columns:1fr 54px}.chart-name{grid-column:1/-1}}
</style>
</head>
<body>
<main>
<header><div class="eyebrow">Local snapshot</div><h1>Copilot Skill Usage</h1>
<p class="lede">Observed skill-load metadata alongside the inventory scanned when this dashboard was generated.</p></header>
<div class="toolbar">
<div class="control-group" id="range-control" role="group" aria-label="Time range">
<span class="control-label">Time range</span><button type="button" data-range="30d" aria-pressed="true">Last 30 days</button><button type="button" data-range="all" aria-pressed="false">All recorded history</button>
</div>
<div class="control-group" id="filter-control" role="group" aria-label="Skill filter">
<span class="control-label">Show</span><button type="button" data-filter="all" aria-pressed="true">All skills</button><button type="button" data-filter="observed" aria-pressed="false">Observed</button><button type="button" data-filter="not-observed" aria-pressed="false">Not observed</button><button type="button" data-filter="unmatched" aria-pressed="false">Unmatched loads</button>
</div></div>
<section class="cards" aria-label="Summary">
<div class="card"><span>Installed skills</span><strong id="installed-count">0</strong></div><div class="card"><span>Recorded loads</span><strong id="load-count">0</strong></div><div class="card"><span>Observed skills</span><strong id="observed-count">0</strong></div><div class="card"><span>Not observed</span><strong id="not-observed-count">0</strong></div></section>
<p id="empty-events" class="empty" hidden>No valid load events are recorded in this range.</p>
<p id="empty-inventory" class="empty" hidden>No skills were found in the current inventory. Unmatched recorded loads may still be listed below.</p>
<section class="panel"><h2>Loads by skill</h2><p class="panel-intro">Each bar is split by load source. Every current inventory skill is included, including zero-load skills.</p>
<div class="legend" aria-label="Load source legend"><span><i class="swatch agent-read"></i>Agent read</span><span><i class="swatch user-slash"></i>User slash</span><span><i class="swatch skill-tool"></i>Skill tool</span></div><div id="chart" class="chart"></div><p id="empty-filtered-chart" class="empty" hidden>No inventoried skills match this filter.</p></section>
<section class="panel"><h2>Skill details</h2><p class="panel-intro">Not observed means no loads were recorded in the selected range; it does not mean a skill is unnecessary.</p>
<div class="table-scroll"><table><thead><tr><th>Skill</th><th>Copies</th><th>Inventory sources</th><th>Agent read</th><th>User slash</th><th>Skill tool</th><th>Total</th><th>Sessions</th><th>Last load</th><th>Status</th></tr></thead><tbody id="skills-table"></tbody></table></div><p id="empty-filtered-table" class="empty" hidden>No inventoried skills match this filter.</p></section>
<section id="unmatched-section" class="panel"><h2>Unmatched loads</h2><p class="panel-intro">An unmatched name has no current inventory match. It may have been removed, renamed, or be outside scanned locations; historical inventory snapshots are not retained, so the cause is unknown.</p>
<div class="table-scroll"><table class="unmatched-table"><thead><tr><th>Recorded skill name</th><th>Loads</th><th>Load sources</th><th>Last seen</th></tr></thead><tbody id="unmatched-table"></tbody></table></div><p id="empty-unmatched" class="empty" hidden>No unmatched loads in this range.</p></section>
<p class="footer">This file is a generated snapshot. It contains skill names and aggregate usage metadata only. No data is uploaded or refreshed automatically.</p>
</main>
<script id="dashboard-data" type="application/json">'''

_HTML_SUFFIX = '''</script>
<script>
"use strict";
const dashboardData = JSON.parse(document.getElementById("dashboard-data").textContent);
const sourceOrder = ["agent-read", "user-slash", "skill-tool"];
let selectedRange = "30d";
let selectedFilter = "all";

function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

function cell(row, value, className) {
    const node = element("td", className || "", value === null || value === undefined || value === "" ? "—" : String(value));
    row.appendChild(node);
}

function render() {
    const view = dashboardData.ranges[selectedRange];
    const summary = view.summary;
    document.getElementById("installed-count").textContent = summary.installedSkills;
    document.getElementById("load-count").textContent = summary.recordedLoads;
    document.getElementById("observed-count").textContent = summary.observedSkills;
    document.getElementById("not-observed-count").textContent = summary.notObservedSkills;
    document.getElementById("empty-events").hidden = summary.recordedLoads > 0;
    document.getElementById("empty-inventory").hidden = summary.installedSkills > 0;

    const skills = selectedFilter === "observed" ? view.skills.filter(skill => skill.observed)
        : selectedFilter === "not-observed" ? view.skills.filter(skill => !skill.observed)
        : selectedFilter === "unmatched" ? [] : view.skills;
    const chart = document.getElementById("chart");
    chart.replaceChildren();
    const table = document.getElementById("skills-table");
    table.replaceChildren();
    const maxTotal = Math.max(1, ...skills.map(skill => skill.total));

    for (const skill of skills) {
        const chartRow = element("div", "chart-row");
        chartRow.appendChild(element("div", "chart-name", skill.name));
        const track = element("div", "bar-track");
        track.setAttribute("role", "img");
        track.setAttribute("aria-label", skill.name + ": " + skill.total + " total loads");
        for (const source of sourceOrder) {
            const count = skill.counts[source] || 0;
            const segment = element("span", "bar-segment " + source);
            segment.style.width = (count / maxTotal * 100) + "%";
            segment.setAttribute("aria-label", source + ": " + count);
            segment.title = source + ": " + count;
            track.appendChild(segment);
        }
        chartRow.appendChild(track);
        chartRow.appendChild(element("span", skill.total ? "bar-total" : "zero-label", skill.total ? skill.total + " loads" : "0 loads"));
        chart.appendChild(chartRow);

        const tr = element("tr");
        cell(tr, skill.name);
        cell(tr, skill.copyCount, "num");
        cell(tr, skill.sources.join(", "), "source-tags");
        for (const source of sourceOrder) cell(tr, skill.counts[source], "num");
        cell(tr, skill.total, "num");
        cell(tr, skill.sessions, "num");
        cell(tr, skill.last ? skill.last.slice(0, 10) : "—");
        const status = element("td");
        status.appendChild(element("span", skill.observed ? "status observed" : "status", skill.observed ? "Observed" : "Not observed"));
        tr.appendChild(status);
        table.appendChild(tr);
    }
    document.getElementById("empty-filtered-chart").hidden = skills.length > 0;
    document.getElementById("empty-filtered-table").hidden = skills.length > 0;

    const unmatchedSection = document.getElementById("unmatched-section");
    unmatchedSection.hidden = selectedFilter !== "all" && selectedFilter !== "unmatched";
    const unmatchedTable = document.getElementById("unmatched-table");
    unmatchedTable.replaceChildren();
    for (const unmatched of view.unmatched) {
        const tr = element("tr");
        cell(tr, unmatched.name);
        cell(tr, unmatched.loads, "num");
        cell(tr, unmatched.sources.join(", "), "source-tags");
        cell(tr, unmatched.last ? unmatched.last.slice(0, 10) : "—");
        unmatchedTable.appendChild(tr);
    }
    document.getElementById("empty-unmatched").hidden = view.unmatched.length > 0;
}

for (const button of document.querySelectorAll("[data-range]")) {
    button.addEventListener("click", () => {
        selectedRange = button.dataset.range;
        document.querySelectorAll("[data-range]").forEach(item => item.setAttribute("aria-pressed", String(item === button)));
        render();
    });
}
for (const button of document.querySelectorAll("[data-filter]")) {
    button.addEventListener("click", () => {
        selectedFilter = button.dataset.filter;
        document.querySelectorAll("[data-filter]").forEach(item => item.setAttribute("aria-pressed", String(item === button)));
        render();
    });
}
render();
</script>
</body>
</html>
'''


def render_html(data):
        import json

        payload = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
        payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
        return _HTML_PREFIX + payload + _HTML_SUFFIX