#!/usr/bin/env python3
"""Generate index.html and status.json from live system state.
Called by lean_runner.sh at session close. Commits and pushes to Pages repo.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────

SITE_DIR = os.path.dirname(os.path.abspath(__file__))
MCP_DIR = os.path.expanduser('~/aadp/mcp-server')
CLAUDIS_DIR = os.path.expanduser('~/aadp/claudis')
SESSIONS_DIR = os.path.join(CLAUDIS_DIR, 'sessions', 'lean')
DIRECTIVES_FILE = os.path.join(CLAUDIS_DIR, 'DIRECTIVES.md')


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


def get_sessions(n=5):
    sessions = []
    try:
        files = sorted(
            [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.md')],
            reverse=True,
        )[:n]
        for fname in files:
            path = os.path.join(SESSIONS_DIR, fname)
            with open(path) as f:
                content = f.read()
            title = fname[11:-3].replace('-', ' ').title() if len(fname) > 14 else fname
            for line in content.splitlines():
                if line.startswith('# '):
                    title = line[2:].strip()
                    break
            # Extract first bullet from What Changed
            outcome = ''
            in_changed = False
            for line in content.splitlines():
                if line.startswith('## What Changed'):
                    in_changed = True
                    continue
                if in_changed and line.startswith('##'):
                    break
                if in_changed and line.strip().startswith('-'):
                    outcome = line.strip().lstrip('- ').strip()
                    # Strip markdown backticks for HTML display
                    outcome = outcome.replace('`', '')
                    if len(outcome) > 150:
                        outcome = outcome[:150] + '…'
                    break
            sessions.append({
                'date': fname[:10],
                'title': title,
                'outcome': outcome,
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


def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def render_session_card(s):
    return f'''  <div class="card">
    <div class="card-title">{esc(s["title"][:80])} &mdash; {esc(s["date"])}</div>
    <div class="card-body">{esc(s["outcome"]) if s["outcome"] else "<em>No summary available.</em>"}</div>
  </div>'''


_NODE_ICONS = {
    'done': '✅', 'in_progress': '🟡', 'pending': '⬜', 'failed': '❌',
}
_TYPE_LABELS = {
    'write': 'write', 'build': 'build', 'research': 'research',
    'verify': 'verify', 'polish': 'polish',
}


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


def generate_html(agent_count, lesson_count, sessions, directive, generated_at, project=None, nodes=None):
    session_cards = '\n'.join(render_session_card(s) for s in sessions) if sessions else \
        '  <div class="card"><div class="card-body">No sessions yet.</div></div>'

    last_card = ''
    if sessions:
        fname_match = sessions[0]['title']
        # Try to extract card ID from title
        import re
        m = re.search(r'B-\d+', sessions[0].get('title', '') + sessions[0].get('date', ''))
        if not m:
            # Try from filename
            files = sorted([f for f in os.listdir(SESSIONS_DIR) if f.endswith('.md')], reverse=True)
            if files:
                m = re.search(r'B-\d+', files[0])
        last_card = m.group(0) if m else '—'

    date_str = generated_at[:10]

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AADP — Autonomous Agent Development Platform</title>
  <style>
    :root {{
      --bg: #0f0f0f; --surface: #1a1a1a; --border: #2a2a2a;
      --text: #e8e8e8; --muted: #888; --accent: #4a9eff;
      --green: #4caf50; --yellow: #ffb300; --red: #f44336;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font-size: 16px; line-height: 1.6;
      max-width: 720px; margin: 0 auto; padding: 24px 16px 48px;
    }}
    h1 {{ font-size: 1.6rem; font-weight: 700; }}
    h2 {{ font-size: 1.1rem; font-weight: 600; color: var(--muted);
          text-transform: uppercase; letter-spacing: .08em; margin: 32px 0 12px; }}
    .tagline {{ color: var(--muted); margin-top: 4px; font-size: .95rem; }}
    .header-meta {{ display: flex; align-items: center; gap: 12px; margin-top: 8px; flex-wrap: wrap; }}
    .dot {{ width: 8px; height: 8px; border-radius: 50%; background: var(--green); display: inline-block; }}
    .badge {{ font-size: .75rem; padding: 2px 8px; border-radius: 12px;
              background: var(--surface); border: 1px solid var(--border); color: var(--muted); }}
    .card {{ background: var(--surface); border: 1px solid var(--border);
             border-radius: 8px; padding: 16px; margin-bottom: 12px; }}
    .card-title {{ font-weight: 600; font-size: .95rem; margin-bottom: 4px; }}
    .card-body {{ font-size: .9rem; color: #ccc; }}
    .next-work {{ background: var(--surface); border: 1px solid var(--accent);
                  border-radius: 8px; padding: 16px; }}
    .next-label {{ font-size: .75rem; text-transform: uppercase; letter-spacing: .08em;
                   color: var(--accent); margin-bottom: 6px; }}
    .next-text {{ font-size: .95rem; }}
    iframe.control {{ width: 100%; border: 1px solid var(--border);
                      border-radius: 8px; background: var(--surface); }}
    .stat-row {{ display: flex; gap: 24px; flex-wrap: wrap; }}
    .stat {{ text-align: center; }}
    .stat-num {{ font-size: 1.5rem; font-weight: 700; color: var(--accent); }}
    .stat-label {{ font-size: .75rem; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }}
    .footer {{ margin-top: 48px; font-size: .8rem; color: var(--muted); text-align: center; }}
    .footer a {{ color: var(--accent); text-decoration: none; }}
    .project-card {{ background: var(--surface); border: 1px solid var(--border);
                     border-radius: 8px; padding: 16px; }}
    .project-title {{ font-weight: 700; font-size: 1rem; margin-bottom: 4px; }}
    .project-goal {{ font-size: .85rem; color: var(--muted); margin-bottom: 12px; }}
    .progress-bar-wrap {{ background: var(--border); border-radius: 4px; height: 6px; margin-bottom: 4px; }}
    .progress-bar {{ background: var(--accent); border-radius: 4px; height: 6px; transition: width .3s; }}
    .progress-label {{ font-size: .75rem; color: var(--muted); margin-bottom: 12px; }}
    .node-list {{ display: flex; flex-direction: column; gap: 6px; }}
    .node-row {{ display: flex; align-items: center; gap: 8px; font-size: .9rem; }}
    .node-icon {{ font-size: 1rem; width: 20px; flex-shrink: 0; }}
    .node-name {{ flex: 1; }}
    .node-type {{ font-size: .7rem; color: var(--muted); background: var(--border);
                  padding: 1px 6px; border-radius: 8px; }}
    @media (max-width: 480px) {{ h1 {{ font-size: 1.3rem; }} .stat-row {{ gap: 16px; }} }}
  </style>
</head>
<body>

  <h1>AADP</h1>
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
    src="https://inborn-rotating-anole.anvil.app#EmbedControl"
    class="control"
    height="480"
    frameborder="0"
    loading="lazy"
    title="AADP System Control"
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
  </div>

  <div class="footer">
    <a href="https://github.com/thompsmanlearn/claudis">claudis repo</a> &nbsp;&middot;&nbsp;
    <a href="status.json">status.json</a> &nbsp;&middot;&nbsp;
    Generated {esc(generated_at[:16].replace('T', ' '))} UTC
  </div>

</body>
</html>'''


def main():
    env = load_env()
    now = datetime.now(timezone.utc).isoformat()

    print('[generate_site] Gathering data...')
    agent_count, lesson_count = get_supabase_counts(env)
    sessions = get_sessions(5)
    directive = get_directive()

    project, nodes = get_project_graph(env)
    print(f'[generate_site] agents={agent_count} lessons={lesson_count} sessions={len(sessions)} nodes={len(nodes)}')

    # Write index.html
    html = generate_html(agent_count, lesson_count, sessions, directive, now, project, nodes)
    with open(os.path.join(SITE_DIR, 'index.html'), 'w') as f:
        f.write(html)

    # Write status.json
    status = {
        'generated_at': now,
        'system': 'online',
        'mode': 'lean',
        'agent_count': agent_count,
        'lesson_count': lesson_count,
        'last_sessions': [
            {'date': s['date'], 'descriptor': s['title'][:60], 'outcome': s['outcome']}
            for s in sessions
        ],
        'current_directive': directive,
    }
    with open(os.path.join(SITE_DIR, 'status.json'), 'w') as f:
        json.dump(status, f, indent=2)

    # Commit and push
    print('[generate_site] Committing and pushing...')
    for cmd in [
        ['git', '-C', SITE_DIR, 'add', 'index.html', 'status.json'],
        ['git', '-C', SITE_DIR, 'commit', '-m', f'site update: {now[:10]}'],
        ['git', '-C', SITE_DIR, 'push', 'origin', 'main'],
    ]:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0 and 'nothing to commit' not in result.stdout + result.stderr:
            print(f'[generate_site] {cmd[2]} warning: {result.stderr.strip()}', file=sys.stderr)

    print(f'[generate_site] Done — {now[:16]} UTC')


if __name__ == '__main__':
    main()
