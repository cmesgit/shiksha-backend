"""Read-only consistency validation for this architecture pack (stdlib only)."""
from pathlib import Path
import json
import re

PACK = Path(__file__).resolve().parent
WORKSPACE = PACK.parents[3]
errors = []
markdown = list(PACK.rglob('*.md'))
for path in markdown:
    body = path.read_text()
    if body.count('```') % 2:
        errors.append(f'{path.name}: unbalanced code fences')
    for target in re.findall(r'\]\(([^)]+)\)', body):
        if target.startswith(('http:', 'https:', 'codex:', '#')):
            continue
        resolved = path.parent / target.split('#', 1)[0]
        if not resolved.exists():
            errors.append(f'{path.name}: missing link {target}')
    for source in re.findall(r'```json\s*\n(.*?)\n```', body, re.S):
        try:
            json.loads(source)
        except ValueError as exc:
            errors.append(f'{path.name}: invalid JSON example {exc}')

tasks = json.loads((PACK / 'tasks/index.json').read_text())
index = {t['id']: t for t in tasks}
if len(index) != len(tasks):
    errors.append('Duplicate task ID')
for task in tasks:
    if not (PACK / task['prompt']).exists():
        errors.append(f"{task['id']}: missing prompt")
    for file in task['existing_files']:
        if not (WORKSPACE / file).exists():
            errors.append(f"{task['id']}: claimed existing file missing: {file}")
    for dependency in task['dependencies']:
        if dependency not in index:
            errors.append(f"{task['id']}: unknown dependency {dependency}")
visited, visiting = set(), set()
def visit(key):
    if key in visiting:
        errors.append(f'Dependency cycle at {key}')
        return
    if key in visited or key not in index:
        return
    visiting.add(key)
    for dependency in index[key]['dependencies']:
        visit(dependency)
    visiting.remove(key)
    visited.add(key)
for key in index:
    visit(key)
for error in errors:
    print('ERROR:', error)
print(f'{len(markdown)} Markdown documents; {len(tasks)} tasks; {len(errors)} consistency errors')
raise SystemExit(bool(errors))
