import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from ntulearn_mcp import workflows as w, server
from ntulearn_mcp.client import NTULearnClient


def fake():
    c = AsyncMock()
    c._base_url = 'https://ntulearn.ntu.edu.sg'
    c.get_course.return_value = {'name': 'EE6221'}
    c.get_course_contents.return_value = [{'id': 'folder', 'title': 'Week 1', 'hasChildren': True}]
    c.get_content_children.return_value = [{'id': 'file1', 'title': 'Assignment 1', 'modified': 'v1'}]
    c.get_content_item.return_value = {'id': 'file1', 'title': 'Assignment 1', 'body': 'Submit by 30 Sep 2026.'}
    c.download_bytes.return_value = (b'Version 1', 'text/plain')
    c.get_gradebook_columns.return_value = []
    c.get_calendar_items.return_value = []
    c.get_announcements.return_value = []
    return c


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_incremental_update_and_local_edits(self):
        c = fake()
        with tempfile.TemporaryDirectory() as td, patch.object(server, '_resolve_content_files', AsyncMock(return_value=({}, None, [('/download', 'slides.txt')]))):
            args = {'course_ids': ['c1'], 'semester': '2026S1', 'destination_dir': td}
            _, preview = await w.sync_materials(c, args)
            self.assertEqual(preview['counts'], {'would_download': 1})
            self.assertFalse((Path(td) / '2026S1').exists())
            c.download_bytes.assert_not_called()
            args['dry_run'] = False
            _, first = await w.sync_materials(c, args)
            self.assertEqual(first['counts'], {'downloaded': 1})
            dest = Path(first['root']) / first['files'][0]['path']
            self.assertIn('Week 1', str(dest))
            _, second = await w.sync_materials(c, args)
            self.assertEqual(second['counts'], {'unchanged': 1})
            self.assertEqual(c.download_bytes.await_count, 1)
            c.get_content_children.return_value[0]['modified'] = 'v2'
            c.download_bytes.return_value = (b'Version 2', 'text/plain')
            _, updated = await w.sync_materials(c, args)
            self.assertEqual(updated['counts'], {'updated': 1})
            self.assertEqual(dest.read_bytes(), b'Version 2')
            dest.write_text('my notes')
            c.get_content_children.return_value[0]['modified'] = 'v3'
            _, conflict = await w.sync_materials(c, args)
            self.assertEqual(conflict['counts'], {'local_conflict': 1})
            self.assertEqual(dest.read_text(), 'my notes')

    async def test_failure_resume_and_signed_url_not_saved(self):
        c = fake()
        with tempfile.TemporaryDirectory() as td, patch.object(server, '_resolve_content_files', AsyncMock(return_value=({}, None, [('/file?secret=abc', 'a.txt'), ('/file2', 'b.txt')]))):
            args = {'course_ids': ['c1'], 'semester': 's1', 'destination_dir': td, 'dry_run': False}
            c.download_bytes.side_effect = [(b'a', 'text/plain'), RuntimeError('fail')]
            _, first = await w.sync_materials(c, args)
            self.assertEqual(first['counts'], {'downloaded': 1, 'failed': 1})
            c.download_bytes.side_effect = None
            _, retry = await w.sync_materials(c, args)
            self.assertEqual(retry['counts'], {'downloaded': 1, 'unchanged': 1})
            self.assertNotIn('secret', (Path(retry['root']) / '.ntulearn-manifest.json').read_text())

    async def test_edit_during_download_is_preserved(self):
        c = fake()
        with tempfile.TemporaryDirectory() as td, patch.object(server, '_resolve_content_files', AsyncMock(return_value=({}, None, [('/file', 'a.txt')]))):
            args = {'course_ids': ['c1'], 'semester': 's1', 'destination_dir': td, 'dry_run': False}
            _, first = await w.sync_materials(c, args)
            dest = Path(first['root']) / first['files'][0]['path']
            c.get_content_children.return_value[0]['modified'] = 'v2'
            async def download(*a, **kw):
                dest.write_text('edit during download')
                return b'new', 'text/plain'
            c.download_bytes.side_effect = download
            _, r = await w.sync_materials(c, args)
            self.assertEqual(r['counts'], {'local_conflict': 1})
            self.assertEqual(dest.read_text(), 'edit during download')

    async def test_manifest_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                w.confined(Path(td), '../escape')
            Path(td, 'link').symlink_to('/tmp')
            with self.assertRaises(ValueError):
                w.confined(Path(td), 'link/escape')

    async def test_cycle_and_partial_folder_failure(self):
        c = fake()
        c.get_content_children.return_value = [{'id': 'folder', 'title': 'loop', 'hasChildren': True}]
        rows, errors, truncated = await w.walk(c, 'c1', 10)
        self.assertEqual(len(rows), 1)
        c.get_content_children.side_effect = RuntimeError('failed folder')
        rows, errors, truncated = await w.walk(c, 'c1', 10)
        self.assertEqual(len(errors), 1)


class AssignmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_merge_conflict_unknown_status_and_evidence(self):
        c = fake()
        c.get_gradebook_columns.return_value = [{'id': 'col1', 'contentId': 'file1', 'name': 'Assignment 1', 'grading': {'due': '2026-09-30T12:00:00Z'}}]
        c.get_user_grades.return_value = [{'columnId': 'col1', 'status': 'NeedsGrading'}]
        c.get_calendar_items.return_value = [{'id': 'e1', 'title': 'Assignment 1', 'start': '2026-10-01T12:00:00Z'}]
        c.get_announcements.return_value = [{'id': 'a1', 'title': 'Correction', 'body': {'rawText': 'Submission deadline is 2 October 2026.'}}]
        _, report = await w.assignments(c, {'course_ids': ['c1'], 'include_documents': False})
        self.assertEqual(len(report['assignments']), 1)
        assignment = report['assignments'][0]
        self.assertTrue(assignment['deadlineConflict'])
        self.assertIsNone(assignment['due'])
        self.assertEqual(assignment['submissionStatus']['raw'], 'NeedsGrading')
        self.assertEqual(len(assignment['sources']), 3)
        self.assertEqual(len(report['deadlineEvidence']), 2)
        self.assertTrue(all(e['needsReview'] for e in report['deadlineEvidence']))

    async def test_same_instant_not_conflict_and_courses_stay_separate(self):
        c = fake()
        c.get_gradebook_columns.return_value = [{'id': 'col1', 'name': 'Assignment 1', 'grading': {'due': '2026-09-30T12:00:00Z'}}]
        c.get_user_grades.return_value = []
        c.get_calendar_items.return_value = [{'id': 'e1', 'title': 'Assignment 1', 'start': '2026-09-30T20:00:00+08:00'}]
        _, r = await w.assignments(c, {'course_ids': ['c1', 'c2'], 'include_documents': False})
        self.assertEqual(len(r['assignments']), 2)
        self.assertFalse(any(a['deadlineConflict'] for a in r['assignments']))
        self.assertTrue(all(a['submissionStatus'] is None for a in r['assignments']))

    async def test_document_evidence_and_budget(self):
        c = fake()
        c.download_bytes.return_value = (b'Deadline: 2026-10-03.', 'text/plain')
        with patch.object(server, '_resolve_content_files', AsyncMock(return_value=({}, None, [('/a', 'brief.txt'), ('/b', 'brief2.txt')]))):
            _, r = await w.assignments(c, {'course_ids': ['c1'], 'max_documents': 1})
        self.assertEqual(r['documentScan'], {'read': 1, 'skipped': 1})
        self.assertTrue(r['truncated'])
        self.assertEqual(r['deadlineEvidence'][-1]['source']['filename'], 'brief.txt')
        self.assertIsNone(r['assignments'][0]['due'])

    async def test_partial_failure_is_visible(self):
        c = fake()
        c.get_calendar_items.side_effect = RuntimeError('server failure')
        _, r = await w.assignments(c, {'course_ids': ['c1'], 'include_documents': False})
        self.assertTrue(r['errors'])
        self.assertEqual(len(r['assignments']), 1)

    async def test_syllabus_filename_and_practice_are_not_assignments(self):
        c = fake()
        c.get_content_children.return_value = [
            {'id': 'syllabus', 'title': 'Course Information', 'contentHandler': {'id': 'resource/x-bb-file', 'file': {'fileName': 'syllabus.pdf'}}},
            {'id': 'practice', 'title': 'Exercises (not for submission)', 'contentHandler': {'id': 'resource/x-bb-file', 'file': {'fileName': 'assignment.pdf'}}}]
        with patch.object(server, '_resolve_content_files', AsyncMock(return_value=({}, None, []))):
            _, r = await w.assignments(c, {'course_ids': ['c1'], 'include_documents': False})
        self.assertEqual(r['assignments'], [])
        self.assertEqual(len(r['deadlineEvidence']), 2)
        self.assertEqual(c.get_content_item.await_count, 2)

    async def test_same_title_distinct_calendar_events_not_collapsed(self):
        c = fake()
        c.get_course_contents.return_value = []
        c.get_calendar_items.return_value = [
            {'id': 'e1', 'title': 'Quiz', 'start': '2026-09-30T12:00:00Z'},
            {'id': 'e2', 'title': 'Quiz', 'start': '2026-10-07T12:00:00Z'}]
        _, r = await w.assignments(c, {'course_ids': ['c1'], 'include_documents': False})
        self.assertEqual(len(r['assignments']), 2)

    async def test_pdf_page_citation(self):
        from tests.test_server import _TINY_PDF_BYTES
        c = fake()
        c.download_bytes.return_value = (_TINY_PDF_BYTES, 'application/pdf')
        with patch.object(server, '_resolve_content_files', AsyncMock(return_value=({}, None, [('/a', 'brief.pdf')]))), patch('pypdf._page.PageObject.extract_text', return_value='Submit by 2026-10-03.'):
            _, r = await w.assignments(c, {'course_ids': ['c1']})
        self.assertEqual(r['deadlineEvidence'][-1]['source']['page'], 1)


class DownloadLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_limit_and_401(self):
        async def handler(request):
            return httpx.Response(200, content=b'123456')
        c = NTULearnClient('https://example.com', 'fake', transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(ValueError):
                await c.download_bytes('/x', max_bytes=3)
            body, _ = await c.download_bytes('/x', max_bytes=10)
            self.assertEqual(body, b'123456')
        finally:
            await c.close()

    async def test_tool_registration(self):
        result = await server.list_tools()
        names = {t.name for t in result}
        self.assertIn('ntulearn_sync_materials', names)
        self.assertIn('ntulearn_get_assignments', names)
