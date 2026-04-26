#!/usr/bin/env python3
"""Generate multi-page site from live system state.
Called by lean_runner.sh at session close. Commits and pushes to Pages repo.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────

SITE_DIR = os.path.dirname(os.path.abspath(__file__))
MCP_DIR = os.path.expanduser('~/aadp/mcp-server')
CLAUDIS_DIR = os.path.expanduser('~/aadp/claudis')
SESSIONS_DIR = os.path.join(CLAUDIS_DIR, 'sessions', 'lean')
DIRECTIVES_FILE = os.path.join(CLAUDIS_DIR, 'DIRECTIVES.md')


# ── Data Loading ──────────────────────────────────────────────────────────────

def load_env():
    env = {}
    path = os.path.join(MCP_DIR, '.env')
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def supabase_get(env, path):
    import urllib.request
    headers = {
        'apikey': env['SUPABASE_SERVICE_KEY'],
        'Authorization': f"Bearer {env['SUPABASE_SERVICE_KEY']}",
    }
    url = env['SUPABASE_URL'] + path
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read())


def get_project_graph(env):
    try:
        projects = supabase_get(env, '/rest/v1/aadp_projects?select=id,name,goal,status&status=eq.active')
        if not projects:
            return None, []
        project = projects[0]
        nodes = supabase_get(env,
            f"/rest/v1/aadp_project_nodes?project_id=eq.{project['id']}"
            "&select=id,name,type,status,dependencies,session_budget&order=created_at.asc"
        )
        return project, nodes
    except Exception as e:
        print(f'[generate_site] Project graph failed: {e}', file=sys.stderr)
        return None, []


def get_supabase_counts(env):
    try:
        import urllib.request
        headers = {
            'apikey': env['SUPABASE_SERVICE_KEY'],
            'Authorization': f"Bearer {env['SUPABASE_SERVICE_KEY']}",
        }
        agent_count, lesson_count = 0, 0
        for table, filt, attr in [
            ('agent_registry', 'status=eq.active', 'agent_count'),
            ('lessons_learned', None, 'lesson_count'),
        ]:
            url = f"{env['SUPABASE_URL']}/rest/v1/{table}?select=count"
            if filt:
                url += f'&{filt}'
            req = urllib.request.Request(url, headers={**headers, 'Prefer': 'count=exact'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                count = int(resp.headers.get('Content-Range', '0/0').split('/')[-1])
                if attr == 'agent_count':
                    agent_count = count
                else:
                    lesson_count = count
        return agent_count, lesson_count
    except Exception as e:
        print(f'[generate_site] Supabase count failed: {e}', file=sys.stderr)
        return 0, 0


def get_sessions(n=None):
    sessions = []
    try:
        files = sorted(
            [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.md')],
            reverse=True,
        )
        if n is not None:
            files = files[:n]
        for fname in files:
            path = os.path.join(SESSIONS_DIR, fname)
            with open(path) as f:
                content = f.read()
            title = fname[11:-3].replace('-', ' ').title() if len(fname) > 14 else fname
            directive = ''
            outcome = ''
            learned = ''
            for line in content.splitlines():
                if line.startswith('# '):
                    title = line[2:].strip()
                    break
            in_directive = in_changed = in_learned = False
            for line in content.splitlines():
                if line.startswith('## Directive'):
                    in_directive = True
                    in_changed = in_learned = False
                    continue
                if line.startswith('## What Changed'):
                    in_changed = True
                    in_directive = in_learned = False
                    continue
                if line.startswith('## What Was Learned'):
                    in_learned = True
                    in_directive = in_changed = False
                    continue
                if line.startswith('## '):
                    in_directive = in_changed = in_learned = False
                    continue
                if in_directive and line.strip() and not directive:
                    directive = line.strip()
                if in_changed and line.strip().startswith('-') and not outcome:
                    outcome = line.strip().lstrip('- ').strip().replace('`', '')
                    if len(outcome) > 150:
                        outcome = outcome[:150] + '…'
                if in_learned and line.strip().startswith('-') and not learned:
                    learned = line.strip().lstrip('- ').strip()
                    if len(learned) > 120:
                        learned = learned[:120] + '…'
            sessions.append({
                'date': fname[:10],
                'title': title,
                'directive': directive,
                'outcome': outcome,
                'learned': learned,
            })
    except Exception as e:
        print(f'[generate_site] Sessions read failed: {e}', file=sys.stderr)
    return sessions


def get_directive():
    try:
        with open(DIRECTIVES_FILE) as f:
            return f.read().strip()
    except Exception:
        return ''


def get_agents(env):
    try:
        return supabase_get(env,
            '/rest/v1/agent_registry'
            '?select=agent_name,display_name,agent_type,description,status,schedule,protected,telegram_command'
            '&order=status.asc,agent_name.asc'
        )
    except Exception as e:
        print(f'[generate_site] Agents failed: {e}', file=sys.stderr)
        return []


def get_capabilities(env):
    try:
        return supabase_get(env,
            '/rest/v1/capabilities'
            '?select=name,category,description,confidence,times_used,last_used'
            '&order=category.asc,name.asc'
        )
    except Exception as e:
        print(f'[generate_site] Capabilities failed: {e}', file=sys.stderr)
        return []


def get_direction_queue(env):
    try:
        return supabase_get(env,
            '/rest/v1/work_queue'
            '?select=task_type,status,priority,input_data,created_at,completed_at'
            '&task_type=neq.explore'
            '&order=created_at.desc'
            '&limit=30'
        )
    except Exception as e:
        print(f'[generate_site] Queue failed: {e}', file=sys.stderr)
        return []


# ── Rendering Helpers ──────────────────────────────────────────────────────────

def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


_NODE_ICONS = {
    'done': '✅', 'in_progress': '🟡', 'pending': '⬜', 'failed': '❌',
}
_TYPE_LABELS = {
    'write': 'write', 'build': 'build', 'research': 'research',
    'verify': 'verify', 'polish': 'polish',
}


def shared_css():
    return '''
    :root {
      --bg: #0f0f0f; --surface: #1a1a1a; --border: #2a2a2a;
      --text: #e8e8e8; --muted: #888; --accent: #4a9eff;
      --green: #4caf50; --yellow: #ffb300; --red: #f44336;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font-size: 16px; line-height: 1.6;
      max-width: 720px; margin: 0 auto; padding: 0 16px 48px;
    }
    h1 { font-size: 1.6rem; font-weight: 700; }
    h2 { font-size: 1.1rem; font-weight: 600; color: var(--muted);
          text-transform: uppercase; letter-spacing: .08em; margin: 32px 0 12px; }
    h3 { font-size: 1rem; font-weight: 600; margin: 20px 0 8px; }
    .tagline { color: var(--muted); margin-top: 4px; font-size: .95rem; }
    .header-meta { display: flex; align-items: center; gap: 12px; margin-top: 8px; flex-wrap: wrap; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); display: inline-block; }
    .badge { font-size: .75rem; padding: 2px 8px; border-radius: 12px;
              background: var(--surface); border: 1px solid var(--border); color: var(--muted); }
    .badge-green { border-color: var(--green); color: var(--green); }
    .badge-yellow { border-color: var(--yellow); color: var(--yellow); }
    .badge-red { border-color: var(--red); color: var(--red); }
    .card { background: var(--surface); border: 1px solid var(--border);
             border-radius: 8px; padding: 16px; margin-bottom: 12px; }
    .card-title { font-weight: 600; font-size: .95rem; margin-bottom: 4px; }
    .card-body { font-size: .9rem; color: #ccc; }
    .stat-row { display: flex; gap: 24px; flex-wrap: wrap; }
    .stat { text-align: center; }
    .stat-num { font-size: 1.5rem; font-weight: 700; color: var(--accent); }
    .stat-label { font-size: .75rem; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
    .footer { margin-top: 48px; font-size: .8rem; color: var(--muted); text-align: center; }
    .footer a { color: var(--accent); text-decoration: none; }
    .project-card { background: var(--surface); border: 1px solid var(--border);
                     border-radius: 8px; padding: 16px; }
    .project-title { font-weight: 700; font-size: 1rem; margin-bottom: 4px; }
    .project-goal { font-size: .85rem; color: var(--muted); margin-bottom: 12px; }
    .progress-bar-wrap { background: var(--border); border-radius: 4px; height: 6px; margin-bottom: 4px; }
    .progress-bar { background: var(--accent); border-radius: 4px; height: 6px; }
    .progress-label { font-size: .75rem; color: var(--muted); margin-bottom: 12px; }
    .node-list { display: flex; flex-direction: column; gap: 6px; }
    .node-row { display: flex; align-items: center; gap: 8px; font-size: .9rem; }
    .node-icon { font-size: 1rem; width: 20px; flex-shrink: 0; }
    .node-name { flex: 1; }
    .node-type { font-size: .7rem; color: var(--muted); background: var(--border);
                  padding: 1px 6px; border-radius: 8px; }
    .next-work { background: var(--surface); border: 1px solid var(--accent);
                  border-radius: 8px; padding: 16px; }
    .next-label { font-size: .75rem; text-transform: uppercase; letter-spacing: .08em;
                   color: var(--accent); margin-bottom: 6px; }
    .next-text { font-size: .95rem; }
    iframe.control { width: 100%; border: 1px solid var(--border);
                      border-radius: 8px; background: var(--surface); }
    nav { display: flex; gap: 4px; padding: 16px 0 24px; flex-wrap: wrap; }
    nav a { font-size: .85rem; padding: 6px 12px; border-radius: 6px;
             border: 1px solid var(--border); color: var(--muted); text-decoration: none; }
    nav a:hover { border-color: var(--accent); color: var(--accent); }
    nav a.active { background: var(--surface); border-color: var(--accent); color: var(--accent); }
    @media (max-width: 480px) { h1 { font-size: 1.3rem; } .stat-row { gap: 16px; } nav { gap: 2px; } }'''


def nav_bar(current_page):
    pages = [
        ('index.html', 'Home'),
        ('fleet.html', 'Fleet'),
        ('capabilities.html', 'Capabilities'),
        ('architecture.html', 'Architecture'),
        ('sessions.html', 'Sessions'),
        ('direction.html', 'Direction'),
    ]
    links = []
    for href, label in pages:
        cls = ' class="active"' if href == current_page else ''
        links.append(f'<a href="{href}"{cls}>{label}</a>')
    return '<nav>' + '\n'.join(links) + '</nav>'


def page_shell(title, current_page, body, generated_at):
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AADP — {esc(title)}</title>
  <style>{shared_css()}</style>
</head>
<body>
{nav_bar(current_page)}
{body}
  <div class="footer">
    <a href="https://github.com/thompsmanlearn/claudis">claudis repo</a> &nbsp;&middot;&nbsp;
    <a href="status.json">status.json</a> &nbsp;&middot;&nbsp;
    Generated {esc(generated_at[:16].replace('T', ' '))} UTC
  </div>
</body>
</html>'''


# ── Page: Index ───────────────────────────────────────────────────────────────

def render_session_card(s):
    return f'''  <div class="card">
    <div class="card-title">{esc(s["title"][:80])} &mdash; {esc(s["date"])}</div>
    <div class="card-body">{esc(s["outcome"]) if s["outcome"] else "<em>No summary available.</em>"}</div>
  </div>'''


def render_project_graph(project, nodes):
    if not project:
        return ''
    done = sum(1 for n in nodes if n['status'] == 'done')
    total = len(nodes)
    pct = int(done / total * 100) if total else 0
    rows = '\n'.join(
        f'    <div class="node-row">'
        f'<span class="node-icon">{_NODE_ICONS.get(n["status"], "⬜")}</span>'
        f'<span class="node-name">{esc(n["name"])}</span>'
        f'<span class="node-type">{esc(_TYPE_LABELS.get(n["type"], n["type"]))}</span>'
        f'</div>'
        for n in nodes
    )
    return f'''  <h2>Active Project</h2>
  <div class="project-card">
    <div class="project-title">{esc(project["name"])}</div>
    <div class="project-goal">{esc(project["goal"][:160])}{"…" if len(project["goal"]) > 160 else ""}</div>
    <div class="progress-bar-wrap">
      <div class="progress-bar" style="width:{pct}%"></div>
    </div>
    <div class="progress-label">{done} of {total} nodes complete</div>
    <div class="node-list">
{rows}
    </div>
  </div>'''


def generate_index(agent_count, lesson_count, sessions, directive, generated_at, project=None, nodes=None):
    session_cards = '\n'.join(render_session_card(s) for s in sessions) if sessions else \
        '  <div class="card"><div class="card-body">No sessions yet.</div></div>'
    last_card = '—'
    try:
        max_num = 0
        for fname in os.listdir(SESSIONS_DIR):
            m = re.search(r'[bB][-_]?(\d+)', fname)
            if m:
                n = int(m.group(1))
                if n > max_num:
                    max_num = n
        if max_num:
            last_card = f'B-{max_num:03d}'
    except Exception:
        pass

    date_str = generated_at[:10]

    body = f'''  <h1>AADP</h1>
  <p class="tagline">Autonomous Agent Development Platform &mdash; a Raspberry Pi 5 that builds itself.</p>
  <div class="header-meta">
    <span class="dot"></span>
    <span style="font-size:.85rem;color:var(--muted)">Online</span>
    <span class="badge">Lean Mode</span>
    <span class="badge">Updated {esc(date_str)}</span>
  </div>

  <h2>System at a Glance</h2>
  <div class="stat-row">
    <div class="stat"><div class="stat-num">{agent_count}</div><div class="stat-label">Active Agents</div></div>
    <div class="stat"><div class="stat-num">{lesson_count}</div><div class="stat-label">Lessons</div></div>
    <div class="stat"><div class="stat-num">6</div><div class="stat-label">Skills</div></div>
    <div class="stat"><div class="stat-num">{esc(last_card)}</div><div class="stat-label">Last Card</div></div>
  </div>

  <h2>Recent Sessions</h2>
{session_cards}

  <h2>Current Focus</h2>
  <div class="next-work">
    <div class="next-label">&#9654; Active Direction</div>
    <div class="next-text">{esc(directive) if directive else "No active directive."}</div>
  </div>

{render_project_graph(project, nodes or [])}

  <h2>Give Direction</h2>
  <iframe
    src="https://inborn-rotating-anole.anvil.app"
    class="control"
    height="900"
    frameborder="0"
    loading="lazy"
    title="AADP Dashboard"
  ></iframe>

  <h2>What This System Is</h2>
  <div class="card">
    <div class="card-body">
      AADP runs on a Raspberry Pi 5 (16GB). It&rsquo;s always on. It runs {agent_count} n8n workflow agents
      that monitor system health, ingest research, synthesize findings, and manage {lesson_count} operational
      lessons. Claude Code is the primary builder &mdash; it reads directives, executes them, writes session
      artifacts, and leaves the system more capable after every session.<br><br>
      The long-term goal: Bill gives a high-level intention and the system autonomously researches,
      decomposes the work, builds capabilities, and executes &mdash; keeping Bill in the loop without
      requiring approval at every step.
    </div>
  </div>'''

    return page_shell('Autonomous Agent Development Platform', 'index.html', body, generated_at)


# ── Page: Fleet ───────────────────────────────────────────────────────────────

_STATUS_COLORS = {'active': 'green', 'paused': 'yellow', 'retired': 'red'}
_TYPE_COLORS = {
    'developer': '#4a9eff', 'critic': '#bb86fc', 'publisher': '#4caf50',
    'scout': '#ff9800', 'analyst': '#26c6da', 'reader': '#aaa', 'router': '#888',
}


def render_agent_card(a):
    status = a.get('status', 'unknown')
    color = _STATUS_COLORS.get(status, '')
    badge_cls = f' badge-{color}' if color else ''
    type_color = _TYPE_COLORS.get(a.get('agent_type', ''), '#aaa')
    protected = ' 🔒' if a.get('protected') else ''
    schedule = a.get('schedule') or 'on_demand'
    desc = (a.get('description') or '')[:200]
    if len(a.get('description') or '') > 200:
        desc += '…'
    return f'''  <div class="card">
    <div style="display:flex;align-items:flex-start;gap:8px;justify-content:space-between;flex-wrap:wrap;">
      <div class="card-title">{esc(a.get("display_name") or a.get("agent_name",""))}{esc(protected)}</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:2px;">
        <span class="badge{badge_cls}">{esc(status)}</span>
        <span class="badge" style="border-color:{type_color};color:{type_color}">{esc(a.get("agent_type",""))}</span>
      </div>
    </div>
    <div class="card-body" style="margin-top:6px;">{esc(desc)}</div>
    <div style="font-size:.75rem;color:var(--muted);margin-top:6px;">⏱ {esc(schedule)}</div>
  </div>'''


def render_agent_compact_row(a):
    type_color = _TYPE_COLORS.get(a.get('agent_type', ''), '#aaa')
    protected = ' 🔒' if a.get('protected') else ''
    return (
        f'  <div style="padding:8px 0;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;">'
        f'<span style="flex:1;font-size:.85rem;">{esc(a.get("display_name") or a.get("agent_name",""))}{esc(protected)}</span>'
        f'<span style="font-size:.7rem;color:{type_color};">{esc(a.get("agent_type",""))}</span>'
        f'</div>'
    )


def generate_fleet_page(agents, generated_at):
    active = [a for a in agents if a.get('status') == 'active']
    paused = [a for a in agents if a.get('status') == 'paused']
    retired = [a for a in agents if a.get('status') == 'retired']

    active_cards = '\n'.join(render_agent_card(a) for a in active) or \
        '<div class="card"><div class="card-body">None.</div></div>'

    paused_rows = '\n'.join(render_agent_compact_row(a) for a in paused) or \
        '<div style="padding:10px 0;color:var(--muted);font-size:.85rem;">None.</div>'
    retired_rows = '\n'.join(render_agent_compact_row(a) for a in retired) or \
        '<div style="padding:10px 0;color:var(--muted);font-size:.85rem;">None.</div>'

    body = f'''  <h1>Agent Fleet</h1>
  <p class="tagline">All agents in the AADP ecosystem, grouped by lifecycle status.</p>

  <h2>System at a Glance</h2>
  <div class="stat-row">
    <div class="stat"><div class="stat-num" style="color:var(--green)">{len(active)}</div><div class="stat-label">Active</div></div>
    <div class="stat"><div class="stat-num" style="color:var(--yellow)">{len(paused)}</div><div class="stat-label">Paused</div></div>
    <div class="stat"><div class="stat-num" style="color:var(--red)">{len(retired)}</div><div class="stat-label">Retired</div></div>
    <div class="stat"><div class="stat-num">{len(agents)}</div><div class="stat-label">Total</div></div>
  </div>

  <h2>Active &mdash; {len(active)} agents</h2>
{active_cards}

  <h2>Paused &mdash; {len(paused)} agents</h2>
  <div class="card" style="padding:0 16px;">
    <div style="padding:8px 0;font-size:.75rem;color:var(--muted);">Not currently scheduled. Reactivate via Telegram or Anvil dashboard.</div>
{paused_rows}
  </div>

  <h2>Retired &mdash; {len(retired)} agents</h2>
  <div class="card" style="padding:0 16px;opacity:.7;">
{retired_rows}
  </div>'''

    return page_shell('Fleet', 'fleet.html', body, generated_at)


# ── Page: Capabilities ────────────────────────────────────────────────────────

def generate_capabilities_page(capabilities, generated_at):
    by_cat = {}
    for cap in capabilities:
        cat = cap.get('category') or 'uncategorized'
        by_cat.setdefault(cat, []).append(cap)

    sections = []
    for cat in sorted(by_cat.keys()):
        caps = by_cat[cat]
        rows = []
        for c in caps:
            conf = c.get('confidence') or 0
            conf_pct = int(conf * 100)
            used = c.get('times_used') or 0
            conf_color = 'var(--green)' if conf >= 0.85 else ('var(--yellow)' if conf >= 0.6 else 'var(--red)')
            rows.append(
                f'    <div style="display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--border);">'
                f'<span style="flex:1;font-size:.85rem;font-weight:500;">{esc(c["name"])}</span>'
                f'<div style="width:72px;background:var(--border);border-radius:3px;height:4px;flex-shrink:0;">'
                f'<div style="width:{conf_pct}%;background:{conf_color};border-radius:3px;height:4px;"></div></div>'
                f'<span style="font-size:.7rem;color:var(--muted);width:28px;text-align:right">{conf_pct}%</span>'
                f'<span style="font-size:.7rem;color:var(--muted);width:32px;text-align:right">{used}×</span>'
                f'</div>'
            )
        cat_label = cat.replace('_', ' ').title()
        sections.append(f'''  <h2>{esc(cat_label)} &mdash; {len(caps)}</h2>
  <div class="card" style="padding:0 16px;">
{"".join(rows)}
  </div>''')

    body = f'''  <h1>Capabilities</h1>
  <p class="tagline">What this system knows how to do. Confidence reflects demonstrated reliability. Updated as capabilities are exercised.</p>

  <h2>Overview</h2>
  <div class="stat-row">
    <div class="stat"><div class="stat-num">{len(capabilities)}</div><div class="stat-label">Capabilities</div></div>
    <div class="stat"><div class="stat-num">{len(by_cat)}</div><div class="stat-label">Categories</div></div>
    <div class="stat"><div class="stat-num">{sum(1 for c in capabilities if (c.get("confidence") or 0) >= 0.85)}</div><div class="stat-label">High Confidence</div></div>
  </div>

{"".join(sections)}'''

    return page_shell('Capabilities', 'capabilities.html', body, generated_at)


# ── Page: Architecture ────────────────────────────────────────────────────────

def generate_architecture_page(generated_at):
    body = '''  <h1>Architecture</h1>
  <p class="tagline">How AADP is built — hardware, services, design philosophy, and key decisions.</p>

  <h2>Hardware</h2>
  <div class="card">
    <div class="card-body">
      <strong>Raspberry Pi 5 &mdash; 16GB RAM.</strong> Always on. All services run locally.
      External API calls are rate-limited resources — treated as such. No cloud compute for the control plane.
    </div>
  </div>

  <h2>Services</h2>
  <div class="card" style="padding:0;">
    <table style="width:100%;border-collapse:collapse;font-size:.9rem;">
      <thead>
        <tr style="border-bottom:1px solid var(--border);">
          <th style="padding:10px 16px;text-align:left;color:var(--muted);font-weight:600;">Service</th>
          <th style="padding:10px 16px;text-align:left;color:var(--muted);font-weight:600;">Location</th>
          <th style="padding:10px 16px;text-align:left;color:var(--muted);font-weight:600;">Role</th>
        </tr>
      </thead>
      <tbody>
        <tr style="border-bottom:1px solid var(--border);">
          <td style="padding:10px 16px;font-weight:500;">n8n 2.6.4</td>
          <td style="padding:10px 16px;color:var(--muted);">localhost:5678</td>
          <td style="padding:10px 16px;color:#ccc;">Workflow automation &mdash; hosts all agent workflows</td>
        </tr>
        <tr style="border-bottom:1px solid var(--border);">
          <td style="padding:10px 16px;font-weight:500;">Supabase</td>
          <td style="padding:10px 16px;color:var(--muted);">Remote (cloud)</td>
          <td style="padding:10px 16px;color:#ccc;">Primary operational database &mdash; all structured data</td>
        </tr>
        <tr style="border-bottom:1px solid var(--border);">
          <td style="padding:10px 16px;font-weight:500;">ChromaDB v0.5.20</td>
          <td style="padding:10px 16px;color:var(--muted);">localhost:8000</td>
          <td style="padding:10px 16px;color:#ccc;">Semantic memory &mdash; lessons, research, session memory</td>
        </tr>
        <tr style="border-bottom:1px solid var(--border);">
          <td style="padding:10px 16px;font-weight:500;">Stats Server</td>
          <td style="padding:10px 16px;color:var(--muted);">localhost:9100</td>
          <td style="padding:10px 16px;color:#ccc;">Host-process bridge &mdash; filesystem, git, GitHub API proxy</td>
        </tr>
        <tr>
          <td style="padding:10px 16px;font-weight:500;">MCP Server</td>
          <td style="padding:10px 16px;color:var(--muted);">~/aadp/mcp-server/</td>
          <td style="padding:10px 16px;color:#ccc;">Claude Code tool access &mdash; all MCP tools route through this</td>
        </tr>
      </tbody>
    </table>
  </div>

  <h2>Supabase Tables</h2>
  <div class="card" style="padding:0;">
    <table style="width:100%;border-collapse:collapse;font-size:.85rem;">
      <thead>
        <tr style="border-bottom:1px solid var(--border);">
          <th style="padding:8px 16px;text-align:left;color:var(--muted);">Table</th>
          <th style="padding:8px 16px;text-align:left;color:var(--muted);">Purpose</th>
        </tr>
      </thead>
      <tbody>
        <tr style="border-bottom:1px solid var(--border);"><td style="padding:8px 16px;font-family:monospace;font-size:.8rem;">work_queue</td><td style="padding:8px 16px;color:#ccc;">Task queue (pending → claimed → complete)</td></tr>
        <tr style="border-bottom:1px solid var(--border);"><td style="padding:8px 16px;font-family:monospace;font-size:.8rem;">agent_registry</td><td style="padding:8px 16px;color:#ccc;">Agent metadata, lifecycle, and status</td></tr>
        <tr style="border-bottom:1px solid var(--border);"><td style="padding:8px 16px;font-family:monospace;font-size:.8rem;">lessons_learned</td><td style="padding:8px 16px;color:#ccc;">Operational lessons with ChromaDB IDs and application counts</td></tr>
        <tr style="border-bottom:1px solid var(--border);"><td style="padding:8px 16px;font-family:monospace;font-size:.8rem;">experimental_outputs</td><td style="padding:8px 16px;color:#ccc;">Agent run outputs and evaluation results</td></tr>
        <tr style="border-bottom:1px solid var(--border);"><td style="padding:8px 16px;font-family:monospace;font-size:.8rem;">audit_log</td><td style="padding:8px 16px;color:#ccc;">System audit trail</td></tr>
        <tr style="border-bottom:1px solid var(--border);"><td style="padding:8px 16px;font-family:monospace;font-size:.8rem;">research_papers</td><td style="padding:8px 16px;color:#ccc;">arXiv papers from the research pipeline</td></tr>
        <tr style="border-bottom:1px solid var(--border);"><td style="padding:8px 16px;font-family:monospace;font-size:.8rem;">error_log</td><td style="padding:8px 16px;color:#ccc;">Unresolved system errors</td></tr>
        <tr style="border-bottom:1px solid var(--border);"><td style="padding:8px 16px;font-family:monospace;font-size:.8rem;">session_notes</td><td style="padding:8px 16px;color:#ccc;">Session handoff notes (consumed once per session)</td></tr>
        <tr style="border-bottom:1px solid var(--border);"><td style="padding:8px 16px;font-family:monospace;font-size:.8rem;">capabilities</td><td style="padding:8px 16px;color:#ccc;">Tracked system capabilities with confidence and usage counts</td></tr>
        <tr><td style="padding:8px 16px;font-family:monospace;font-size:.8rem;">aadp_projects / aadp_project_nodes</td><td style="padding:8px 16px;color:#ccc;">Project graph — active work decomposed into typed nodes</td></tr>
      </tbody>
    </table>
  </div>

  <h2>ChromaDB Collections</h2>
  <div class="card">
    <div class="card-body" style="display:flex;flex-wrap:wrap;gap:8px;">
      <span class="badge" style="font-family:monospace;">lessons_learned</span>
      <span class="badge" style="font-family:monospace;">research_findings</span>
      <span class="badge" style="font-family:monospace;">session_memory</span>
      <span class="badge" style="font-family:monospace;">reference_material</span>
      <span class="badge" style="font-family:monospace;">error_patterns</span>
      <span class="badge" style="font-family:monospace;">agent_templates</span>
    </div>
  </div>

  <h2>Design Principles</h2>
  <div class="card">
    <div class="card-body">
      <p style="margin-bottom:10px;"><strong>Supabase is the primary store.</strong>
      All structured operational data lives in Supabase. ChromaDB is for semantic retrieval only &mdash;
      lessons, research, and session memory that need embedding-based lookup.</p>

      <p style="margin-bottom:10px;"><strong>Webhook-only agents (V2 architecture).</strong>
      n8n workflows have a single Webhook trigger. Scheduling is external (via Sentinel scheduler).
      This prevents dual-trigger conflicts and makes timing auditable.</p>

      <p style="margin-bottom:10px;"><strong>Claude Code as the primary builder.</strong>
      Claude Code reads directives, has full MCP tool access, writes session artifacts,
      and commits changes to GitHub. It is not an agent in the n8n fleet &mdash;
      it operates at a higher abstraction level than any agent it builds.</p>

      <p style="margin-bottom:10px;"><strong>Context economy.</strong>
      Every token in a persistent artifact must change what a future instance does.
      Bootstrap context is kept under 4,000 tokens. Verbose identity documents have been retired.</p>

      <p><strong>Dual output convention.</strong>
      Every interaction produces one output for the consumer (Bill or the system) and one for the
      future (session artifact, lesson, capability update). Learning is a byproduct, not an overhead.</p>
    </div>
  </div>

  <h2>Agent Lifecycle</h2>
  <div class="card">
    <div class="card-body">
      <p>Agents follow a <strong>sandbox &rarr; active &rarr; retired/paused</strong> lifecycle.</p>
      <ul style="padding-left:20px;margin-top:8px;font-size:.9rem;color:#ccc;">
        <li style="margin-bottom:6px;"><strong>Sandbox:</strong> Built and being tested. Evaluated against 4 pillars (behavior, output quality, reliability, integration fit) before promotion.</li>
        <li style="margin-bottom:6px;"><strong>Active:</strong> Running in production. Monitored by agent_health_monitor for consecutive failures.</li>
        <li style="margin-bottom:6px;"><strong>Paused:</strong> Temporarily deactivated. Preserved for reactivation.</li>
        <li><strong>Retired:</strong> Superseded or no longer needed. Preserved for reference.</li>
      </ul>
    </div>
  </div>

  <h2>Destinations</h2>
  <div class="card">
    <div class="card-body">
      <ol style="padding-left:20px;font-size:.9rem;color:#ccc;">
        <li style="margin-bottom:8px;">The system can receive a high-level intention from Bill and autonomously research, build needed capabilities, and begin execution — without Bill directing each step.</li>
        <li style="margin-bottom:8px;">The system can publish professional-quality games on Roblox.</li>
        <li style="margin-bottom:8px;">Bill has a functional interface for monitoring trajectory, providing direction, and reviewing system output.</li>
        <li style="margin-bottom:8px;">The system detects and recovers from its own failures without Bill discovering them first.</li>
        <li>Bill has a proper visual dashboard and interactive controls accessible from any device.</li>
      </ol>
    </div>
  </div>'''

    return page_shell('Architecture', 'architecture.html', body, generated_at)


# ── Page: Sessions ────────────────────────────────────────────────────────────

def generate_sessions_page(sessions, generated_at):
    SHOW = 30
    shown = sessions[:SHOW]
    total = len(sessions)

    if not shown:
        body_content = '<div class="card"><div class="card-body">No sessions yet.</div></div>'
    else:
        # Group by month
        by_month = {}
        for s in shown:
            month = s['date'][:7]  # YYYY-MM
            try:
                datetime.strptime(month, '%Y-%m')
            except ValueError:
                continue  # skip files without a YYYY-MM-DD date prefix
            by_month.setdefault(month, []).append(s)

        sections = []
        for month in sorted(by_month.keys(), reverse=True):
            month_label = datetime.strptime(month, '%Y-%m').strftime('%B %Y')
            rows = []
            for s in by_month[month]:
                directive_snip = ''
                if s.get('directive'):
                    d = s['directive'][:100]
                    if len(s['directive']) > 100:
                        d += '…'
                    directive_snip = (
                        f'<div style="font-size:.8rem;color:var(--muted);margin-top:3px;">'
                        f'{esc(d)}</div>'
                    )
                outcome_snip = ''
                if s.get('outcome'):
                    outcome_snip = (
                        f'<div style="font-size:.8rem;color:#bbb;margin-top:3px;">'
                        f'{esc(s["outcome"])}</div>'
                    )
                rows.append(
                    f'  <div style="padding:10px 0;border-bottom:1px solid var(--border);">'
                    f'<div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;">'
                    f'<span style="font-weight:600;font-size:.9rem;">{esc(s["title"][:70])}</span>'
                    f'<span style="font-size:.75rem;color:var(--muted);">{esc(s["date"])}</span>'
                    f'</div>'
                    f'{directive_snip}{outcome_snip}'
                    f'</div>'
                )
            sections.append(
                f'  <h2>{esc(month_label)}</h2>\n'
                f'  <div class="card" style="padding:0 16px;">\n'
                + '\n'.join(rows) +
                '\n  </div>'
            )
        body_content = '\n'.join(sections)
        if total > SHOW:
            body_content += (
                f'\n  <div class="card" style="opacity:.6;">'
                f'<div class="card-body">Showing {SHOW} of {total} sessions.</div></div>'
            )

    body = f'''  <h1>Session Log</h1>
  <p class="tagline">Lean sessions, most recent first. Each executes one backlog card and writes a structured artifact.</p>

  <div class="stat-row" style="margin-bottom:24px;">
    <div class="stat"><div class="stat-num">{total}</div><div class="stat-label">Total Sessions</div></div>
    <div class="stat"><div class="stat-num">{sum(1 for s in sessions if s.get("outcome"))}</div><div class="stat-label">With Outcomes</div></div>
  </div>

{body_content}'''

    return page_shell('Sessions', 'sessions.html', body, generated_at)


# ── Page: Direction ────────────────────────────────────────────────────────────

def generate_direction_page(directive, sessions, queue_items, generated_at):
    # Current directive
    current_html = f'''  <h2>Current Directive</h2>
  <div class="next-work">
    <div class="next-label">&#9654; Active Direction</div>
    <div class="next-text">{esc(directive) if directive else "No active directive."}</div>
  </div>'''

    # Direction history from sessions (last 20, most recent first)
    history_cards = []
    for s in sessions[:20]:
        if not s.get('directive'):
            continue
        history_cards.append(f'''  <div class="card">
    <div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;">
      <span class="badge" style="font-size:.7rem;">{esc(s["date"])}</span>
      <span style="font-size:.8rem;color:var(--muted);">{esc((s.get("title") or "")[:60])}</span>
    </div>
    <div class="card-body" style="margin-top:6px;">{esc(s["directive"][:200])}{"…" if len(s.get("directive","")) > 200 else ""}</div>
  </div>''')
    history_html = '\n'.join(history_cards) or '<div class="card"><div class="card-body">No history yet.</div></div>'

    # Notable queue items (non-explore tasks)
    queue_cards = []
    for item in queue_items[:15]:
        status = item.get('status', '')
        status_color = 'var(--green)' if status == 'complete' else ('var(--yellow)' if status == 'pending' else 'var(--muted)')
        idata = item.get('input_data') or {}
        if isinstance(idata, str):
            try:
                idata = json.loads(idata)
            except Exception:
                idata = {}
        desc = idata.get('description') or idata.get('request') or idata.get('diagnostic') or ''
        if not desc:
            continue
        if len(desc) > 200:
            desc = desc[:200] + '…'
        created = (item.get('created_at') or '')[:10]
        task_type = item.get('task_type', '')
        queue_cards.append(f'''  <div class="card">
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px;">
      <span class="badge" style="font-size:.7rem;">{esc(task_type)}</span>
      <span style="font-size:.75rem;color:{status_color};">●</span>
      <span style="font-size:.75rem;color:var(--muted);">{esc(status)} &mdash; {esc(created)}</span>
    </div>
    <div class="card-body">{esc(desc)}</div>
  </div>''')

    queue_html = '\n'.join(queue_cards) or '<div class="card"><div class="card-body">No directed queue items.</div></div>'

    body = f'''{current_html}

  <h2>Direction History</h2>
  <div class="card" style="border-color:var(--border);">
    <div class="card-body" style="font-size:.85rem;color:var(--muted);">
      What Bill directed at each session. Most recent first.
    </div>
  </div>
{history_html}

  <h2>Notable Queue Items</h2>
  <div class="card" style="border-color:var(--border);">
    <div class="card-body" style="font-size:.85rem;color:var(--muted);">
      Recent non-explore work queue items showing bill-directed or diagnostic tasks.
    </div>
  </div>
{queue_html}'''

    return page_shell('Direction', 'direction.html', body, generated_at)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    env = load_env()
    now = datetime.now(timezone.utc).isoformat()

    print('[generate_site] Gathering data...')
    agent_count, lesson_count = get_supabase_counts(env)
    sessions = get_sessions()
    directive = get_directive()
    project, nodes = get_project_graph(env)
    agents = get_agents(env)
    capabilities = get_capabilities(env)
    queue_items = get_direction_queue(env)

    print(f'[generate_site] agents={agent_count} lessons={lesson_count} sessions={len(sessions)} '
          f'fleet={len(agents)} capabilities={len(capabilities)} nodes={len(nodes)}')

    # Generate all pages
    pages = {
        'index.html': generate_index(agent_count, lesson_count, sessions[:5], directive, now, project, nodes),
        'fleet.html': generate_fleet_page(agents, now),
        'capabilities.html': generate_capabilities_page(capabilities, now),
        'architecture.html': generate_architecture_page(now),
        'sessions.html': generate_sessions_page(sessions, now),
        'direction.html': generate_direction_page(directive, sessions, queue_items, now),
    }

    for filename, content in pages.items():
        with open(os.path.join(SITE_DIR, filename), 'w') as f:
            f.write(content)
        print(f'[generate_site] wrote {filename}')

    # Write status.json
    status = {
        'generated_at': now,
        'system': 'online',
        'mode': 'lean',
        'agent_count': agent_count,
        'lesson_count': lesson_count,
        'last_sessions': [
            {'date': s['date'], 'descriptor': s['title'][:60], 'outcome': s['outcome']}
            for s in sessions[:5]
        ],
        'current_directive': directive,
    }
    with open(os.path.join(SITE_DIR, 'status.json'), 'w') as f:
        json.dump(status, f, indent=2)

    # Commit and push all files
    print('[generate_site] Committing and pushing...')
    files_to_add = list(pages.keys()) + ['status.json']
    for cmd in [
        ['git', '-C', SITE_DIR, 'add'] + files_to_add,
        ['git', '-C', SITE_DIR, 'commit', '-m', f'site update: {now[:10]}'],
        ['git', '-C', SITE_DIR, 'push', 'origin', 'main'],
    ]:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0 and 'nothing to commit' not in result.stdout + result.stderr:
            print(f'[generate_site] {cmd[2]} warning: {result.stderr.strip()}', file=sys.stderr)

    print(f'[generate_site] Done — {now[:16]} UTC')


if __name__ == '__main__':
    main()
