from django.test import TestCase
from django.contrib.auth import get_user_model
import pandas as pd

from .models import Table, Workspace
from .tools import get_metadata, execute_code, revert_version

User = get_user_model()


class TablesEndToEndTestCase(TestCase):
    """Simple end-to-end test for tables functionality using real E2B execution"""

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')

    def test_complete_workflow(self):
        """Test complete workflow: create tables, get metadata, execute code, verify updates"""
        # Step 1: Create a table from DataFrame
        df_original = pd.DataFrame({
            'name': ['Alice', 'Bob', 'Charlie'],
            'score': [85, 90, 95]
        })
        table = Table.create_from_dataframe('users', df_original, self.user, description='User scores')

        # Verify table creation with actual data
        self.assertEqual(table.name, 'users')
        self.assertEqual(table.version, 1)
        self.assertEqual(table.row_count, 3)
        self.assertEqual(table.updated_role, 'user')

        # Verify initial data
        initial_df = table.load_dataframe()
        self.assertEqual(initial_df['score'].tolist(), [85, 90, 95])
        self.assertEqual(initial_df['name'].tolist(), ['Alice', 'Bob', 'Charlie'])

        # Step 2: Get metadata with preview
        metadata = get_metadata(table=table, head=2)
        self.assertEqual(metadata['name'], 'users')
        self.assertEqual(metadata['row_count'], 3)
        self.assertEqual(metadata['columns'], ['name', 'score'])
        self.assertIn('head_rows', metadata)
        self.assertEqual(len(metadata['head_rows']), 2)
        # Verify preview data
        self.assertEqual(metadata['head_rows'][0]['score'], 85)
        self.assertEqual(metadata['head_rows'][1]['score'], 90)

        # Step 3: Execute code to modify table (actually runs on E2B)
        result = execute_code(
            code="print(f'Before: {table[\"score\"].tolist()}'); table['score'] = table['score'] + 5; print(f'After: {table[\"score\"].tolist()}')",
            table=table
        )

        # Step 4: Verify execution result
        self.assertIsNone(result['error'], f"Execution error: {result.get('error')}")
        self.assertTrue(result['persisted'], f"Not persisted. Persist error: {result.get('persist_error')}")
        self.assertEqual(result['new_version'], 2)
        # Verify stdout shows the transformation
        self.assertIn('Before:', '\n'.join(result['stdout']))
        self.assertIn('After:', '\n'.join(result['stdout']))

        # Step 5: Verify table was updated with correct data
        table.refresh_from_db()
        self.assertEqual(table.version, 2)
        self.assertEqual(table.updated_role, 'assistant')

        loaded_df = table.load_dataframe()
        # Scores should be increased by 5
        self.assertEqual(loaded_df['score'].tolist(), [90, 95, 100])
        # Names should be unchanged
        self.assertEqual(loaded_df['name'].tolist(), ['Alice', 'Bob', 'Charlie'])

        # Step 6: Test workspace with multiple tables
        df2 = pd.DataFrame({'id': [1, 2, 3], 'value': [10, 20, 30]})
        table2 = Table.create_from_dataframe('values', df2, self.user)

        workspace = Workspace.objects.create(name='analysis', owner=self.user)
        workspace.tables.add(table, table2)

        # Get workspace metadata
        ws_metadata = get_metadata(workspace=workspace)
        self.assertEqual(ws_metadata['workspace_name'], 'analysis')
        self.assertEqual(len(ws_metadata['tables']), 2)
        self.assertIn('table_1', ws_metadata['tables'])
        self.assertIn('table_2', ws_metadata['tables'])

        # Step 7: Test version revert
        df_modified2 = pd.DataFrame({
            'name': ['Alice', 'Bob', 'Charlie'],
            'score': [95, 100, 105]
        })
        table.save_dataframe(df_modified2, increment_version=True)
        self.assertEqual(table.version, 3)

        # Revert to version 2
        revert_result = revert_version(table=table)
        self.assertTrue(revert_result['success'])

        # Verify data reverted
        table.refresh_from_db()
        reverted_df = table.load_dataframe()
        self.assertEqual(reverted_df['score'].tolist(), [90, 95, 100])
