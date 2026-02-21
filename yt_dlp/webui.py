from __future__ import annotations

import argparse
import http.server
import json
import optparse
import os
import re
import subprocess
import sys
import threading
import urllib.parse
import webbrowser

from .options import create_parser
from .utils import shell_quote, write_string

_FAVORITES_FILE_NAME = '.yt_dlp_webui_favorites.json'
_CUSTOM_VALUE = 'custom'
_UNSET_VALUE = 'unset'
_SET_VALUE = 'set'
_CHOICE_PREFIX = 'choice|'


def _favorites_path():
    return os.path.join(os.getcwd(), _FAVORITES_FILE_NAME)


def _clean_help_text(text):
    return re.sub(r'\s+', ' ', text).strip()


def _make_option_id(option, seen, fallback_index):
    base = (option._long_opts or option._short_opts or [f'option_{fallback_index}'])[0]
    base = re.sub(r'[^A-Za-z0-9_-]+', '_', base.lstrip('-'))
    option_id = base
    suffix = 2
    while option_id in seen:
        option_id = f'{base}_{suffix}'
        suffix += 1
    seen.add(option_id)
    return option_id


def _format_option_display(option):
    parts = []
    if option._short_opts:
        parts.append(option._short_opts[0])
    if option._long_opts:
        long_opt = option._long_opts[0]
        if option.takes_value():
            long_opt = f'{long_opt} {option.metavar}'
        parts.append(long_opt)
    elif option.takes_value() and parts:
        parts[-1] = f'{parts[-1]} {option.metavar}'
    return ', '.join(parts)


def _build_schema():
    parser = create_parser()
    groups = []
    all_options = []
    seen_ids = set()

    for group in parser.option_groups:
        options = []
        for option in group.option_list:
            if option.help == optparse.SUPPRESS_HELP:
                continue
            if '--help' in option._long_opts or '--version' in option._long_opts:
                continue

            option_id = _make_option_id(option, seen_ids, len(all_options) + 1)
            primary_flag = option._long_opts[0] if option._long_opts else option._short_opts[0]
            option_data = {
                'id': option_id,
                'group': group.title,
                'primary_flag': primary_flag,
                'display': _format_option_display(option),
                'help': _clean_help_text(option.help),
                'takes_value': option.takes_value(),
                'metavar': option.metavar if option.takes_value() else '',
                'choices': list(option.choices or ()),
            }
            options.append(option_data)
            all_options.append(option_data)
        groups.append({
            'title': group.title,
            'options': options,
        })
    return groups, all_options


SCHEMA_GROUPS, SCHEMA_OPTIONS = _build_schema()


def _build_option_arguments(selection_map):
    args = []
    errors = []

    if not isinstance(selection_map, dict):
        return args, ['Invalid option payload']

    for option in SCHEMA_OPTIONS:
        selection = selection_map.get(option['id'])
        if not isinstance(selection, dict):
            continue

        selected = selection.get('select', _UNSET_VALUE)
        if not isinstance(selected, str):
            errors.append(f'Invalid selection for {option["display"]}')
            continue

        if option['takes_value']:
            if selected == _UNSET_VALUE:
                continue

            value = None
            if selected == _CUSTOM_VALUE:
                value = str(selection.get('custom', '')).strip()
            elif selected.startswith(_CHOICE_PREFIX):
                value = selected[len(_CHOICE_PREFIX):]
                if value not in option['choices']:
                    errors.append(f'Invalid selection for {option["display"]}')
                    continue
            else:
                errors.append(f'Invalid selection for {option["display"]}')
                continue

            if not value:
                errors.append(f'Missing value for {option["display"]}')
                continue

            args.extend((option['primary_flag'], value))
            continue

        if selected == _SET_VALUE:
            args.append(option['primary_flag'])
        elif selected not in {_UNSET_VALUE, _SET_VALUE}:
            errors.append(f'Invalid selection for {option["display"]}')

    return args, errors


def _parse_urls(raw_urls):
    if not isinstance(raw_urls, str):
        return []
    return [line.strip() for line in raw_urls.splitlines() if line.strip()]


def _build_command(payload):
    if not isinstance(payload, dict):
        raise ValueError('Invalid request body')

    option_args, errors = _build_option_arguments(payload.get('options', {}))
    urls = _parse_urls(payload.get('urls', ''))

    if not urls:
        errors.append('Provide at least one URL')
    if errors:
        raise ValueError('; '.join(errors))

    return [sys.executable, '-m', 'yt_dlp', *option_args, *urls]


def _load_favorites():
    path = _favorites_path()
    try:
        with open(path, encoding='utf-8') as file:
            loaded = json.load(file)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(loaded, list):
        return []

    favorites = []
    for item in loaded:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name', '')).strip()
        settings = item.get('settings')
        if not name or not isinstance(settings, dict):
            continue
        favorites.append({'name': name, 'settings': settings})
    return favorites


def _save_favorites(favorites):
    path = _favorites_path()
    temp_path = f'{path}.tmp'
    with open(temp_path, 'w', encoding='utf-8', newline='\n') as file:
        json.dump(favorites, file, ensure_ascii=True, indent=2)
        file.write('\n')
    os.replace(temp_path, path)


class _WebUIHandler(http.server.BaseHTTPRequestHandler):
    server_version = 'yt-dlp-webui/1.0'

    def log_message(self, fmt, *args):
        write_string(f'[webui] {self.address_string()} {fmt % args}\n')

    def _send_json(self, payload, status=200):
        raw = json.dumps(payload, ensure_ascii=True).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, text):
        raw = text.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self):
        try:
            content_length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise ValueError('Invalid content length')
        raw = self.rfile.read(content_length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode('utf-8'))
        except json.JSONDecodeError:
            raise ValueError('Request body must be valid JSON')
        if not isinstance(data, dict):
            raise ValueError('Request body must be a JSON object')
        return data

    def _send_error(self, message, status=400):
        self._send_json({'error': message}, status=status)

    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path

        if path == '/':
            self._send_html(_INDEX_HTML)
            return
        if path == '/api/schema':
            self._send_json({
                'groups': SCHEMA_GROUPS,
                'favorites_file': _favorites_path(),
            })
            return
        if path == '/api/favorites':
            self._send_json({'favorites': _load_favorites()})
            return
        if path == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
            return
        self._send_error('Not found', status=404)

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            payload = self._read_json()
        except ValueError as err:
            self._send_error(str(err))
            return

        if path == '/api/preview':
            try:
                command = _build_command(payload)
            except ValueError as err:
                self._send_error(str(err))
                return
            self._send_json({
                'command': shell_quote(command),
                'arguments': command,
            })
            return

        if path == '/api/run':
            try:
                command = _build_command(payload)
            except ValueError as err:
                self._send_error(str(err))
                return

            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            self._send_json({
                'command': shell_quote(command),
                'arguments': command,
                'returncode': completed.returncode,
                'output': completed.stdout or '',
            })
            return

        if path == '/api/favorites':
            name = str(payload.get('name', '')).strip()
            settings = payload.get('settings')
            if not name:
                self._send_error('Favorite name is required')
                return
            if not isinstance(settings, dict):
                self._send_error('Favorite settings are invalid')
                return

            favorites = _load_favorites()
            existing_index = next((i for i, item in enumerate(favorites) if item['name'] == name), None)
            entry = {'name': name, 'settings': settings}
            if existing_index is None:
                favorites.append(entry)
            else:
                favorites[existing_index] = entry
            _save_favorites(favorites)
            self._send_json({'favorites': favorites})
            return

        self._send_error('Not found', status=404)

    def do_DELETE(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        prefix = '/api/favorites/'
        if not path.startswith(prefix):
            self._send_error('Not found', status=404)
            return

        name = urllib.parse.unquote(path[len(prefix):]).strip()
        if not name:
            self._send_error('Favorite name is required')
            return

        favorites = _load_favorites()
        updated = [item for item in favorites if item['name'] != name]
        if len(updated) == len(favorites):
            self._send_error('Favorite not found', status=404)
            return
        _save_favorites(updated)
        self._send_json({'favorites': updated})


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog='python -m yt_dlp.webui',
        description='Run a local browser-based GUI for yt-dlp options.',
    )
    parser.add_argument('--host', default='127.0.0.1', help='Host interface to bind (default: 127.0.0.1)')
    parser.add_argument('--port', type=int, default=8787, help='Port to bind (default: 8787)')
    parser.add_argument('--no-browser', action='store_true', help='Do not automatically open a browser tab')
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    server = http.server.ThreadingHTTPServer((args.host, args.port), _WebUIHandler)
    url = f'http://{args.host}:{args.port}/'

    write_string(f'yt-dlp web UI running at {url}\n')
    write_string(f'Favorites file: {_favorites_path()}\n')

    if not args.no_browser:
        timer = threading.Timer(0.3, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        write_string('\nShutting down yt-dlp web UI\n')
    finally:
        server.server_close()


_INDEX_HTML = '''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>yt-dlp Web UI</title>
  <style>
    :root {
      --bg: #f8fafc;
      --panel: #ffffff;
      --line: #dbe2ea;
      --text: #0f172a;
      --muted: #475569;
      --accent: #0f766e;
      --accent-2: #0ea5a8;
      --danger: #b91c1c;
      --ok: #166534;
    }
    body {
      margin: 0;
      font-family: "Segoe UI", "Noto Sans", sans-serif;
      color: var(--text);
      background:
        radial-gradient(1000px 600px at -10% -20%, #d4f5f5 0%, transparent 60%),
        radial-gradient(900px 500px at 110% -20%, #fef3c7 0%, transparent 55%),
        var(--bg);
    }
    .wrap {
      max-width: 1260px;
      margin: 0 auto;
      padding: 20px 16px 28px;
    }
    h1 {
      margin: 0 0 8px;
      font-size: 28px;
      font-weight: 700;
      letter-spacing: 0.2px;
    }
    .subtitle {
      margin: 0 0 16px;
      color: var(--muted);
      font-size: 14px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      margin-bottom: 12px;
      box-shadow: 0 4px 16px rgba(2, 6, 23, 0.05);
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 12px;
      align-items: end;
    }
    .field {
      grid-column: span 12;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .field.half {
      grid-column: span 6;
    }
    .field.third {
      grid-column: span 4;
    }
    label {
      font-size: 13px;
      color: var(--muted);
    }
    input[type="text"], textarea, select {
      border: 1px solid #c8d2dd;
      border-radius: 8px;
      font-size: 14px;
      padding: 8px 10px;
      background: #fff;
      color: var(--text);
      box-sizing: border-box;
      width: 100%;
    }
    textarea {
      min-height: 84px;
      resize: vertical;
    }
    button {
      border: 0;
      border-radius: 8px;
      padding: 9px 12px;
      font-size: 14px;
      cursor: pointer;
      color: #fff;
      background: linear-gradient(120deg, var(--accent), var(--accent-2));
    }
    button.secondary {
      background: #334155;
    }
    button.danger {
      background: var(--danger);
    }
    button:disabled {
      opacity: 0.65;
      cursor: not-allowed;
    }
    .row-buttons {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .status {
      min-height: 20px;
      font-size: 13px;
      color: var(--muted);
      padding-top: 2px;
    }
    .status.error {
      color: var(--danger);
    }
    .status.ok {
      color: var(--ok);
    }
    details {
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
      margin-bottom: 10px;
      overflow: hidden;
    }
    summary {
      cursor: pointer;
      padding: 10px 12px;
      font-weight: 600;
      background: #f8fafc;
      border-bottom: 1px solid var(--line);
      list-style: none;
    }
    summary::-webkit-details-marker {
      display: none;
    }
    .group-body {
      padding: 8px;
    }
    .option-row {
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      margin-bottom: 8px;
      padding: 8px;
      display: grid;
      grid-template-columns: 1fr 280px;
      gap: 10px;
      align-items: start;
      background: #ffffff;
    }
    .option-row.configured {
      border-color: #67e8f9;
      background: #f0fdfa;
    }
    .option-name {
      font-family: Consolas, "Courier New", monospace;
      font-size: 13px;
      margin-bottom: 4px;
      word-break: break-word;
    }
    .option-help {
      font-size: 12px;
      color: var(--muted);
      line-height: 1.35;
    }
    .option-controls {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .command-preview, .output {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #0b1220;
      color: #dbeafe;
      padding: 10px;
      font-family: Consolas, "Courier New", monospace;
      font-size: 12px;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .command-preview {
      min-height: 46px;
    }
    .output {
      min-height: 140px;
      max-height: 420px;
      overflow: auto;
    }
    .info {
      font-size: 12px;
      color: var(--muted);
    }
    @media (max-width: 920px) {
      .field.half, .field.third {
        grid-column: span 12;
      }
      .option-row {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>yt-dlp Local Web UI</h1>
    <p class="subtitle">Every visible yt-dlp option is generated from the parser and can be configured via dropdown.</p>

    <section class="panel">
      <div class="grid">
        <div class="field">
          <label for="urls">URLs (one per line)</label>
          <textarea id="urls" placeholder="https://example.com/video"></textarea>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="grid">
        <div class="field third">
          <label for="favorite-name">Favorite name</label>
          <input id="favorite-name" type="text" placeholder="My preset">
        </div>
        <div class="field third">
          <label for="favorite-select">Saved favorites</label>
          <select id="favorite-select"></select>
        </div>
        <div class="field third">
          <label for="option-search">Search options</label>
          <input id="option-search" type="text" placeholder="format, audio, subtitles, ...">
        </div>
      </div>
      <div class="row-buttons" style="margin-top:10px;">
        <button id="save-favorite">Save favorite</button>
        <button class="secondary" id="load-favorite">Load favorite</button>
        <button class="danger" id="delete-favorite">Delete favorite</button>
        <button class="secondary" id="preview-command">Preview command</button>
        <button id="run-command">Run yt-dlp</button>
        <label style="display:inline-flex;align-items:center;gap:6px;margin-left:8px;">
          <input id="configured-only" type="checkbox">
          Show configured only
        </label>
      </div>
      <div id="status" class="status"></div>
      <div id="favorites-path" class="info"></div>
    </section>

    <section class="panel">
      <div class="field">
        <label>Generated command</label>
        <pre id="command-preview" class="command-preview">Command preview will appear here.</pre>
      </div>
    </section>

    <section class="panel">
      <div id="groups"></div>
    </section>

    <section class="panel">
      <div class="field">
        <label>Output</label>
        <pre id="output" class="output">Run output will appear here.</pre>
      </div>
    </section>
  </div>

  <script>
    const state = {
      groups: [],
      options: [],
      favorites: [],
    };

    const byId = (id) => document.getElementById(id);

    const optionControlId = (optionId) => `option-${optionId}`;
    const customControlId = (optionId) => `custom-${optionId}`;

    function setStatus(message, kind = '') {
      const el = byId('status');
      el.textContent = message;
      el.className = `status ${kind}`.trim();
    }

    function addSelectOption(select, value, label) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    }

    function collectSettings() {
      const options = {};
      for (const option of state.options) {
        const selectEl = byId(optionControlId(option.id));
        if (!selectEl) {
          continue;
        }
        const selected = selectEl.value;
        const customEl = byId(customControlId(option.id));
        const custom = customEl ? customEl.value : '';

        if (selected !== 'unset' || custom.trim()) {
          options[option.id] = {
            select: selected,
            custom: custom,
          };
        }
      }
      return {
        urls: byId('urls').value,
        options,
      };
    }

    function isConfigured(option) {
      const selectEl = byId(optionControlId(option.id));
      if (!selectEl) {
        return false;
      }
      const selected = selectEl.value;
      if (selected === 'unset') {
        return false;
      }
      if (!option.takes_value || selected.startsWith('choice|')) {
        return selected === 'set' || selected.startsWith('choice|');
      }
      if (selected === 'custom') {
        const customEl = byId(customControlId(option.id));
        return !!(customEl && customEl.value.trim());
      }
      return false;
    }

    function updateOptionRowState(row, option) {
      const selectEl = byId(optionControlId(option.id));
      const customEl = byId(customControlId(option.id));
      if (customEl) {
        customEl.style.display = selectEl.value === 'custom' ? '' : 'none';
      }
      row.classList.toggle('configured', isConfigured(option));
    }

    function createOptionRow(option) {
      const row = document.createElement('div');
      row.className = 'option-row';
      row.dataset.search = `${option.display} ${option.help}`.toLowerCase();

      const meta = document.createElement('div');
      const name = document.createElement('div');
      name.className = 'option-name';
      name.textContent = option.display;
      const help = document.createElement('div');
      help.className = 'option-help';
      help.textContent = option.help || '(No description)';
      meta.appendChild(name);
      meta.appendChild(help);

      const controls = document.createElement('div');
      controls.className = 'option-controls';

      const select = document.createElement('select');
      select.id = optionControlId(option.id);
      addSelectOption(select, 'unset', 'Not set');
      if (option.takes_value) {
        for (const choice of option.choices) {
          addSelectOption(select, `choice|${choice}`, choice);
        }
        addSelectOption(select, 'custom', `Custom ${option.metavar || 'value'}`);
      } else {
        addSelectOption(select, 'set', `Set ${option.primary_flag}`);
      }

      const custom = document.createElement('input');
      custom.type = 'text';
      custom.id = customControlId(option.id);
      custom.placeholder = option.metavar ? `Enter ${option.metavar}` : 'Enter value';
      custom.style.display = 'none';

      select.addEventListener('change', () => {
        updateOptionRowState(row, option);
        applyFilters();
      });
      custom.addEventListener('input', () => {
        updateOptionRowState(row, option);
        applyFilters();
      });

      controls.appendChild(select);
      if (option.takes_value) {
        controls.appendChild(custom);
      }

      row.appendChild(meta);
      row.appendChild(controls);
      return row;
    }

    function applyFilters() {
      const query = byId('option-search').value.trim().toLowerCase();
      const configuredOnly = byId('configured-only').checked;

      for (const details of byId('groups').querySelectorAll('details')) {
        const rows = Array.from(details.querySelectorAll('.option-row'));
        let visible = 0;
        for (const row of rows) {
          const queryMatch = !query || row.dataset.search.includes(query);
          const configuredMatch = !configuredOnly || row.classList.contains('configured');
          row.style.display = queryMatch && configuredMatch ? '' : 'none';
          if (row.style.display !== 'none') {
            visible += 1;
          }
        }
        details.style.display = visible ? '' : 'none';
      }
    }

    function renderGroups() {
      const container = byId('groups');
      container.innerHTML = '';
      for (const group of state.groups) {
        const details = document.createElement('details');
        details.open = group.title === 'General Options';

        const summary = document.createElement('summary');
        summary.textContent = `${group.title} (${group.options.length})`;
        details.appendChild(summary);

        const body = document.createElement('div');
        body.className = 'group-body';
        for (const option of group.options) {
          body.appendChild(createOptionRow(option));
        }
        details.appendChild(body);
        container.appendChild(details);
      }
      applyFilters();
    }

    function refreshFavoriteSelect() {
      const select = byId('favorite-select');
      select.innerHTML = '';
      if (!state.favorites.length) {
        addSelectOption(select, '', 'No saved favorites');
        select.disabled = true;
        return;
      }

      select.disabled = false;
      for (const favorite of state.favorites) {
        addSelectOption(select, favorite.name, favorite.name);
      }
    }

    function applySettings(settings) {
      byId('urls').value = typeof settings.urls === 'string' ? settings.urls : '';

      for (const option of state.options) {
        const select = byId(optionControlId(option.id));
        const custom = byId(customControlId(option.id));
        if (select) {
          select.value = 'unset';
        }
        if (custom) {
          custom.value = '';
        }
      }

      const selectedOptions = settings.options || {};
      for (const option of state.options) {
        const selected = selectedOptions[option.id];
        if (!selected || typeof selected !== 'object') {
          continue;
        }
        const select = byId(optionControlId(option.id));
        const custom = byId(customControlId(option.id));
        if (!select) {
          continue;
        }
        if (typeof selected.select === 'string') {
          const match = Array.from(select.options).some((opt) => opt.value === selected.select);
          if (match) {
            select.value = selected.select;
          }
        }
        if (custom && typeof selected.custom === 'string') {
          custom.value = selected.custom;
        }
      }

      for (const option of state.options) {
        const row = byId(optionControlId(option.id)).closest('.option-row');
        updateOptionRowState(row, option);
      }
      applyFilters();
    }

    async function requestJson(url, init = {}) {
      const response = await fetch(url, {
        headers: {'Content-Type': 'application/json'},
        ...init,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.error || `Request failed: ${response.status}`);
      }
      return data;
    }

    async function loadSchema() {
      const data = await requestJson('/api/schema');
      state.groups = data.groups;
      state.options = data.groups.flatMap((group) => group.options);
      byId('favorites-path').textContent = `Favorites file: ${data.favorites_file}`;
      renderGroups();
      for (const option of state.options) {
        const row = byId(optionControlId(option.id)).closest('.option-row');
        updateOptionRowState(row, option);
      }
    }

    async function loadFavorites() {
      const data = await requestJson('/api/favorites');
      state.favorites = data.favorites || [];
      refreshFavoriteSelect();
    }

    async function previewCommand() {
      const data = await requestJson('/api/preview', {
        method: 'POST',
        body: JSON.stringify(collectSettings()),
      });
      byId('command-preview').textContent = data.command;
      setStatus('Command preview updated.', 'ok');
    }

    async function runCommand() {
      byId('run-command').disabled = true;
      setStatus('Running yt-dlp...', '');
      byId('output').textContent = 'Running...';
      try {
        const data = await requestJson('/api/run', {
          method: 'POST',
          body: JSON.stringify(collectSettings()),
        });
        byId('command-preview').textContent = data.command;
        byId('output').textContent = `$ ${data.command}\n[exit ${data.returncode}]\n\n${data.output || ''}`;
        setStatus(`Completed with exit code ${data.returncode}.`, data.returncode === 0 ? 'ok' : 'error');
      } finally {
        byId('run-command').disabled = false;
      }
    }

    async function saveFavorite() {
      const name = byId('favorite-name').value.trim();
      if (!name) {
        setStatus('Enter a favorite name before saving.', 'error');
        return;
      }
      const data = await requestJson('/api/favorites', {
        method: 'POST',
        body: JSON.stringify({
          name,
          settings: collectSettings(),
        }),
      });
      state.favorites = data.favorites || [];
      refreshFavoriteSelect();
      byId('favorite-select').value = name;
      setStatus(`Saved favorite "${name}".`, 'ok');
    }

    function loadFavoriteFromSelect() {
      const name = byId('favorite-select').value;
      if (!name) {
        setStatus('Select a favorite to load.', 'error');
        return;
      }
      const favorite = state.favorites.find((entry) => entry.name === name);
      if (!favorite) {
        setStatus('Favorite not found.', 'error');
        return;
      }
      applySettings(favorite.settings || {});
      byId('favorite-name').value = favorite.name;
      setStatus(`Loaded favorite "${favorite.name}".`, 'ok');
    }

    async function deleteFavorite() {
      const name = byId('favorite-select').value;
      if (!name) {
        setStatus('Select a favorite to delete.', 'error');
        return;
      }
      const data = await requestJson(`/api/favorites/${encodeURIComponent(name)}`, {
        method: 'DELETE',
      });
      state.favorites = data.favorites || [];
      refreshFavoriteSelect();
      setStatus(`Deleted favorite "${name}".`, 'ok');
    }

    function wireEvents() {
      byId('option-search').addEventListener('input', applyFilters);
      byId('configured-only').addEventListener('change', applyFilters);

      byId('preview-command').addEventListener('click', async () => {
        try {
          await previewCommand();
        } catch (error) {
          setStatus(error.message, 'error');
        }
      });

      byId('run-command').addEventListener('click', async () => {
        try {
          await runCommand();
        } catch (error) {
          setStatus(error.message, 'error');
          byId('output').textContent = error.message;
        }
      });

      byId('save-favorite').addEventListener('click', async () => {
        try {
          await saveFavorite();
        } catch (error) {
          setStatus(error.message, 'error');
        }
      });

      byId('load-favorite').addEventListener('click', loadFavoriteFromSelect);

      byId('delete-favorite').addEventListener('click', async () => {
        try {
          await deleteFavorite();
        } catch (error) {
          setStatus(error.message, 'error');
        }
      });
    }

    async function init() {
      try {
        wireEvents();
        await loadSchema();
        await loadFavorites();
        setStatus('Ready.', 'ok');
      } catch (error) {
        setStatus(error.message || String(error), 'error');
      }
    }

    init();
  </script>
</body>
</html>
'''


if __name__ == '__main__':
    main()
