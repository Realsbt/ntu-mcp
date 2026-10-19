"""Bounded student workflows; no school-side writes or automatic scheduling."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from mcp.types import Tool
from ntulearn_mcp.client import BbRouterExpiredError


def tools():
    courses = {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 30}
    common = {'course_ids': courses, 'max_items': {'type': 'integer', 'minimum': 1, 'maximum': 2000, 'default': 500}}
    return [
        Tool(name='ntulearn_sync_materials', description='Preview or incrementally sync course files into semester/course/original folders. Defaults to dry_run=true. Uses a manifest and checksums, preserves local edits, never deletes files. Inspect errors and truncated before claiming a complete sync.',
             inputSchema={'type': 'object', 'properties': {**common,
                 'semester': {'type': 'string', 'minLength': 1},
                 'destination_dir': {'type': 'string', 'minLength': 1},
                 'dry_run': {'type': 'boolean', 'default': True},
                 'verify_remote': {'type': 'boolean', 'default': False, 'description': 'Re-download to compare hashes even when modification timestamps match.'}},
                 'required': ['semester', 'destination_dir'], 'additionalProperties': False},
             annotations={'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': True}),
        Tool(name='ntulearn_get_assignments', description='Aggregate gradebook, calendar and assignment pages with source links, exact-match merging, explicit submission status and conflicting deadlines. Includes separate unverified deadline excerpts from announcements and assignment documents; never treat excerpts as confirmed deadlines. Check errors/truncated/documentScan for coverage.',
             inputSchema={'type': 'object', 'properties': {**common,
                 'include_documents': {'type': 'boolean', 'default': True},
                 'max_documents': {'type': 'integer', 'minimum': 0, 'maximum': 30, 'default': 10}},
                 'additionalProperties': False},
             annotations={'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': True})]


def now():
    return datetime.now(timezone.utc).isoformat()


def component(value):
    value = re.sub(r'[\x00-\x1f/\\:*?"<>|]', '_', str(value)).strip(' .')[:90]
    return value or 'untitled'


def token(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:12]


def bound(args, name, default, maximum):
    v = args.get(name, default)
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= maximum:
        raise ValueError(f'{name} must be an integer in 0..{maximum}')
    if name == 'max_items' and v == 0:
        raise ValueError('max_items must be positive')
    return v


async def course_ids(client, args):
    from ntulearn_mcp import server
    ids = args.get('course_ids')
    if ids is None:
        ids = await server._resolve_enrolled_course_ids(client)
    if not isinstance(ids, list) or not all(isinstance(x, str) and x for x in ids) or len(ids) > 30:
        raise ValueError('course_ids must contain at most 30 nonempty strings')
    return list(dict.fromkeys(ids))


def error(errors, context, exc):
    if isinstance(exc, BbRouterExpiredError):
        raise exc
    # Do not persist upstream response bodies, cookies or signed URLs in manifests.
    errors.append({'context': context, 'errorType': type(exc).__name__})


async def walk(client, cid, maximum):
    """Return bounded content tree, with visible partial-failure/truncation state."""
    rows, errors, seen = [], [], set()
    queue = [(None, [], 0)]
    truncated = False
    while queue:
        parent, folders, depth = queue.pop(0)
        try:
            items = await (client.get_content_children(cid, parent) if parent else client.get_course_contents(cid))
        except Exception as exc:
            error(errors, f'{cid}/{parent or "root"}', exc)
            continue
        for item in items:
            iid = item.get('id')
            if not iid or iid in seen:
                continue
            if len(rows) >= maximum:
                truncated = True
                break
            seen.add(iid)
            rows.append((item, folders))
            if item.get('hasChildren'):
                if depth >= 15:
                    truncated = True
                else:
                    folder = component(item.get('title') or iid) + '--' + token(iid)
                    queue.append((iid, folders + [folder], depth + 1))
        if len(rows) >= maximum and queue:
            truncated = True
            break
    return rows, errors, truncated


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.ntulearn-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def confined(root, relative):
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Unsafe manifest or destination path')
    return path


@contextmanager
def sync_lock(root, dry):
    if dry:
        yield
        return
    import fcntl
    root.mkdir(parents=True, exist_ok=True)
    lock = confined(root, '.ntulearn-sync.lock')
    with lock.open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another sync is running in this destination') from exc
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


async def sync_materials(client, args):
    from ntulearn_mcp import server
    if not isinstance(args.get('semester'), str) or not args['semester'].strip():
        raise ValueError('semester is required')
    if not isinstance(args.get('destination_dir'), str) or not args['destination_dir'].strip():
        raise ValueError('destination_dir is required')
    root = Path(args['destination_dir']).expanduser().resolve() / component(args['semester'])
    root = root.resolve()
    dry = args.get('dry_run', True)
    verify = args.get('verify_remote', False)
    maximum = bound(args, 'max_items', 500, 2000)
    ids = await course_ids(client, args)
    report = {'dryRun': dry, 'root': str(root), 'files': [], 'errors': [], 'truncated': False, 'checkedAt': now()}
    with sync_lock(root, dry):
        manifest_path = confined(root, '.ntulearn-manifest.json')
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {'version': 1, 'files': {}}
        if manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict):
            raise ValueError('Unsupported or corrupt sync manifest')
        for cid in ids:
            try:
                course = await client.get_course(cid)
            except Exception as exc:
                error(report['errors'], cid, exc)
                course = {'name': cid}
            folder = component(course.get('name') or cid) + '--' + token(cid)
            rows, errors, truncated = await walk(client, cid, maximum)
            report['errors'].extend(errors)
            report['truncated'] |= truncated
            for item, folders in rows:
                iid = item['id']
                if item.get('hasChildren'):
                    continue
                try:
                    _, _, pairs = await server._resolve_content_files(client, cid, iid)
                except Exception as exc:
                    error(report['errors'], f'{cid}/{iid}', exc)
                    continue
                for index, (url, filename) in enumerate(pairs):
                    # Query strings may contain rotating signatures: never store them.
                    identity = filename or urlsplit(url).path
                    key = token(f'{cid}/{iid}/{identity}/{index}')
                    old = manifest['files'].get(key)
                    filename = component(filename or Path(urlsplit(url).path).name or 'download')
                    p = Path(filename)
                    name = f'{p.stem}--{key}{p.suffix}'
                    relative = old['path'] if old else str(Path(folder, *folders, name))
                    result = {'courseId': cid, 'contentId': iid, 'filename': filename, 'path': relative}
                    try:
                        dest = confined(root, relative)
                        local_hash = hashlib.sha256(dest.read_bytes()).hexdigest() if dest.is_file() else None
                        if dest.exists() and (not old or local_hash != old['sha256']):
                            result['status'] = 'local_conflict'
                        elif old and local_hash and item.get('modified') and old.get('modified') == item['modified'] and not verify:
                            result['status'] = 'unchanged'
                        elif dry:
                            result['status'] = 'would_check' if old else 'would_download'
                        else:
                            data, _ = await client.download_bytes(url, max_bytes=100 * 1024 * 1024)
                            digest = hashlib.sha256(data).hexdigest()
                            # Check again after the network await so edits during download survive.
                            current = hashlib.sha256(dest.read_bytes()).hexdigest() if dest.is_file() else None
                            if current != local_hash:
                                result['status'] = 'local_conflict'
                            else:
                                if digest != local_hash:
                                    atomic(dest, data)
                                manifest['files'][key] = {'path': relative, 'sha256': digest, 'modified': item.get('modified'), 'checkedAt': now()}
                                atomic(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode())
                                result['status'] = 'unchanged' if digest == local_hash else ('updated' if old else 'downloaded')
                    except Exception as exc:
                        error(report['errors'], f'{cid}/{iid}/{filename}', exc)
                        result['status'] = 'failed'
                    report['files'].append(result)
    report['counts'] = {status: sum(f['status'] == status for f in report['files']) for status in sorted({f['status'] for f in report['files']})}
    return server._emit(report)


ASSIGNMENT = re.compile(r'assignment|assessment|coursework|project|quiz|brief|syllabus|course outline|作业|测验|考核|课程大纲', re.I)
DEADLINE = re.compile(r'deadline|due\b|submit|submission|截止|提交', re.I)
DATE = re.compile(r'\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b(?:\s+\d{4})?|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,?\s+\d{4})?|\d{1,2}月\d{1,2}日', re.I)


def snippets(text, source):
    # Keep ambiguous dates as evidence; never guess locale/year/time or auto-merge.
    found = []
    for line in re.split(r'\n|(?<=[.!?])\s+', text):
        if DEADLINE.search(line) and DATE.search(line):
            found.append({'source': source, 'excerpt': line[:1000], 'dateText': DATE.findall(line), 'needsReview': True})
        if len(found) >= 20:
            break
    return found


def date_key(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else value
    except (TypeError, ValueError):
        return str(value)


async def assignments(client, args):
    from ntulearn_mcp import server
    ids = await course_ids(client, args)
    maximum = bound(args, 'max_items', 500, 2000)
    max_docs = bound(args, 'max_documents', 10, 30)
    report = {'assignments': [], 'deadlineEvidence': [], 'errors': [], 'truncated': False,
              'checkedAt': now(), 'documentScan': {'read': 0, 'skipped': 0},
              'notes': ['Document/announcement excerpts are unverified; missing dates or submission status mean unknown. No OCR; inspect errors and coverage before concluding no work is due.']}
    for cid in ids:
        groups = []
        def source(kind, ident, title):
            base = client._base_url
            url = f'{base}/webapps/blackboard/execute/content/contentList?course_id={quote(cid)}&content_id={quote(str(ident))}' if kind in ('content', 'document') else f'{base}/ultra/courses/{quote(cid)}/outline'
            return {'kind': kind, 'id': ident, 'title': title, 'url': url, 'courseId': cid}
        def add(title, ident, kind, due=None, aliases=(), status=None, description=None):
            normalized = re.sub(r'\s+', ' ', str(title or '').strip().casefold())
            keys = {str(a) for a in aliases if a}
            candidates = [g for g in groups if keys & g['_aliases']]
            if not candidates:
                candidates = [g for g in groups if normalized and normalized == g['_title'] and not any(s['kind'] == kind for s in g['sources'])]
            if len(candidates) == 1:
                group = candidates[0]
            else:
                group = {'courseId': cid, 'title': title or str(ident), '_title': normalized, '_aliases': set(), 'sources': [], 'deadlines': [], 'submissionStatus': None}
                groups.append(group)
            group['_aliases'].update(keys)
            src = source(kind, ident, title)
            group['sources'].append(src)
            if due:
                group['deadlines'].append({'value': due, 'source': src, 'inferred': False})
            if status is not None:
                group['submissionStatus'] = {'raw': status, 'source': src}
            if description:
                group.setdefault('instructions', []).append({'text': description[:5000], 'source': src})
            return src
        async def fetch(label, fn):
            try:
                return await fn()
            except Exception as exc:
                error(report['errors'], f'{cid}/{label}', exc)
                return []
        columns = await fetch('gradebook', lambda: client.get_gradebook_columns(cid))
        grades = []
        if columns:
            try:
                uid = await client.get_my_user_id()
                grades = await client.get_user_grades(cid, uid)
            except Exception as exc:
                error(report['errors'], f'{cid}/submission-status', exc)
        grade_map = {g.get('columnId'): g for g in grades}
        for col in columns:
            # Calculated total columns are not assignments.
            if not col.get('contentId') and not (col.get('grading') or {}).get('due'):
                continue
            add(col.get('name') or col.get('displayName'), col.get('id'), 'gradebook', (col.get('grading') or {}).get('due'),
                [col.get('id'), col.get('contentId')], grade_map.get(col.get('id'), {}).get('status'))
        calendar = await fetch('calendar', lambda: client.get_calendar_items(course_id=cid, item_type='GradebookColumn'))
        for event in calendar:
            props = event.get('dynamicCalendarItemProps') or {}
            add(event.get('title'), event.get('id'), 'calendar', event.get('start'), [props.get('contentId'), props.get('gradebookColumnId')])
        rows, errors, truncated = await walk(client, cid, maximum)
        report['errors'].extend(errors)
        report['truncated'] |= truncated
        for item, _ in rows:
            iid, title = item['id'], item.get('title') or ''
            handler_info = item.get('contentHandler') or {}
            handler = handler_info.get('id', '')
            filename_hint = (handler_info.get('file') or {}).get('fileName', '')
            relevant = handler == 'resource/x-bb-document' or bool(ASSIGNMENT.search(title + ' ' + handler + ' ' + filename_hint)) or any(iid in g['_aliases'] for g in groups)
            if item.get('hasChildren') or not relevant:
                continue
            details = await fetch(f'content/{iid}', lambda: client.get_content_item(cid, iid))
            if not isinstance(details, dict):
                continue
            desc = details.get('description') or ''
            if isinstance(desc, dict):
                desc = desc.get('rawText') or ''
            body = server._strip_html(details.get('body') or desc)
            is_assignment = (bool(re.search(r'assignment|assessment|course\s*work|project|quiz|作业|测验|考核', title + ' ' + handler + ' ' + filename_hint, re.I)) and not re.search(r'not for submission|optional|不[需用]提交', title, re.I)) or any(iid in g['_aliases'] for g in groups)
            src = add(title, iid, 'content', aliases=[iid], description=body) if is_assignment else source('content', iid, title)
            report['deadlineEvidence'].extend(snippets(body, src))
            try:
                _, _, pairs = await server._resolve_content_files(client, cid, iid)
                for g in groups:
                    if iid in g['_aliases']:
                        g.setdefault('attachments', []).extend({'filename': filename or Path(urlsplit(url).path).name, 'source': src} for url, filename in pairs)
                if not args.get('include_documents', True):
                    continue
                for url, filename in pairs:
                    if report['documentScan']['read'] >= max_docs:
                        report['documentScan']['skipped'] += 1
                        report['truncated'] = True
                        continue
                    name = filename or Path(urlsplit(url).path).name
                    if Path(name).suffix.lower() not in ('.pdf', '.docx', '.pptx', '.txt', '.html'):
                        report['documentScan']['skipped'] += 1
                        continue
                    report['documentScan']['read'] += 1
                    data, content_type = await client.download_bytes(url, max_bytes=10 * 1024 * 1024)
                    doc_src = {**source('document', iid, title), 'filename': name}
                    if Path(name).suffix.lower() == '.pdf':
                        from io import BytesIO
                        from pypdf import PdfReader
                        reader = PdfReader(BytesIO(data))
                        if len(reader.pages) > 100:
                            report['truncated'] = True
                        for n, page in enumerate(reader.pages[:100], 1):
                            text = page.extract_text() or ''
                            report['deadlineEvidence'].extend(snippets(text, {**doc_src, 'page': n}))
                        if not any((p.extract_text() or '').strip() for p in reader.pages[:1]):
                            report['errors'].append({'context': f'{cid}/{iid}/{name}', 'errorType': 'PossiblyScannedPDF'})
                    else:
                        entry = server._extract_content(name, content_type, data)
                        if entry.get('error'):
                            report['errors'].append({'context': f'{cid}/{iid}/{name}', 'errorType': 'DocumentExtractionFailed'})
                        report['deadlineEvidence'].extend(snippets(entry.get('text', ''), doc_src))
            except Exception as exc:
                error(report['errors'], f'{cid}/{iid}/documents', exc)
        announcements = await fetch('announcements', lambda: client.get_announcements(cid))
        for a in announcements:
            body = a.get('body') or ''
            if isinstance(body, dict):
                body = body.get('rawText') or ''
            report['deadlineEvidence'].extend(snippets(server._strip_html(body), source('announcement', a.get('id'), a.get('title'))))
        for group in groups:
            group.pop('_title'); group.pop('_aliases')
            group['deadlineConflict'] = len({date_key(d['value']) for d in group['deadlines']}) > 1
            group['due'] = group['deadlines'][0]['value'] if group['deadlines'] and not group['deadlineConflict'] else None
            report['assignments'].append(group)
    report['assignments'].sort(key=lambda g: date_key(g['due']) if g['due'] else 'z')
    return server._emit(report)
