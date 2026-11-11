import unittest
from types import SimpleNamespace

from hermes_codex_learning.workflow_review import WorkflowReviewGuard, restrict_agent


class WorkflowReviewTests(unittest.TestCase):
    def setUp(self):
        self.guard = WorkflowReviewGuard(None, None)
        self.guard.active['w'] = {'owned': {'workflow-known'}, 'existing': {'workflow-known', 'private'}}

    def call(self, tool, args):
        return self.guard.pre_tool(session_id='w', tool_name=tool, args=args)

    def test_private_reads_memory_and_executable_resources_are_denied(self):
        for name, args in [('memory', {'action': 'add', 'content': 'x'}),
                           ('skill_view', {'name': 'private'}),
                           ('read_file', {'path': '/private'}),
                           ('skills_list', {}),
                           ('skill_manage', {'name': 'workflow-known', 'action': 'write_file',
                                             'file_path': 'run.py', 'file_content': 'pass'})]:
            with self.subTest(name=name):
                self.assertEqual(self.call(name, args)['action'], 'block')

    def test_prose_create_and_owned_revision_use_native_tools(self):
        body = '---\nlearning_scope: global_candidate\n---\nCheck results.'
        self.assertIsNone(self.call('skill_manage', {'action': 'create', 'name': 'workflow-new', 'content': body}))
        self.assertIsNone(self.call('skill_view', {'name': 'workflow-new'}))
        self.assertIsNone(self.call('skill_manage', {'action': 'edit', 'name': 'workflow-known', 'content': body}))
        self.assertEqual(self.call('skill_manage', {'action': 'create', 'name': 'private', 'content': body})['action'], 'block')
        self.assertIsNone(self.guard.pre_tool(session_id='normal', tool_name='memory', args={}))

    def test_prompt_rebuild_and_tool_surface_never_expose_existing_private_context(self):
        parent = SimpleNamespace(_memory_store='PRIVATE', _memory_manager='PRIVATE',
                                 tools=[{'function': {'name': name}} for name in ['memory', 'skill_manage', 'skill_view', 'read_file']])
        restrict_agent(parent, 'fixed prompt')
        self.assertEqual(parent._build_system_prompt(), 'fixed prompt')
        self.assertIsNone(parent._memory_store)
        self.assertIsNone(parent._memory_manager)
        self.assertEqual(parent.valid_tool_names, {'skill_manage', 'skill_view'})

    def test_native_batch_contract_is_checked_before_any_operation_is_authorized(self):
        body = '---\nlearning_scope: global_candidate\n---\nCheck results.'
        good = {'name': 'workflow-new', 'action': 'create', 'content': body}
        bad = {'name': 'private', 'action': 'delete'}
        self.assertEqual(self.call('skill_manage', {'operations': [good, bad]})['action'], 'block')
        self.assertNotIn('workflow-new', self.guard.active['w']['owned'])
        self.assertIsNone(self.call('skill_manage', {'operations': [good]}))
