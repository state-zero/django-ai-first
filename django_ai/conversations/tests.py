# tests/test_conversation_integration.py

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase

from django_ai.conversations.models import ConversationSession, ConversationMessage
from django_ai.conversations.registry import register_agent
from django_ai.automation.workflows.core import engine
from django_ai.automation.queues.sync_executor import SynchronousExecutor

# Import crud to ensure models are registered with StateZero
from tests import crud

User = get_user_model()


class TestAgent:
    """Simple test agent for integration testing"""

    def get_response(self, message, session_context):
        return f"Echo: {message}"


class ConversationStateZeroIntegrationTest(APITestCase):
    """End-to-end test of conversation system via StateZero API"""

    def setUp(self):
        # Set up synchronous executor for testing
        engine.set_executor(SynchronousExecutor())

        # Create test user and authenticate
        self.user = User.objects.create_user(username="testuser", password="password")
        self.client.force_authenticate(user=self.user)

        # Register test agent
        register_agent("test_support", TestAgent)

    def test_create_message_triggers_agent_processing(self):
        """Test that creating a message via StateZero triggers agent processing"""

        # 1. Create a conversation session first
        session_payload = {
            "ast": {
                "query": {
                    "type": "create",
                    "data": {"agent_path": "test_support", "context": {"test": "data"}},
                }
            }
        }

        session_url = reverse(
            "statezero:model_view", args=["conversations.ConversationSession"]
        )
        session_response = self.client.post(
            session_url, data=session_payload, format="json"
        )

        print("Session response status:", session_response.status_code)
        print("Session response data:", session_response.data)

        self.assertEqual(session_response.status_code, 200)

        # StateZero returns data in data.data array, not data.id
        session_ids = session_response.data.get("data", {}).get("data", [])
        self.assertTrue(len(session_ids) > 0, "Should have created a session")
        session_id = str(session_ids[0])  # Convert UUID to string

        print("Extracted session_id:", session_id)
        self.assertIsNotNone(session_id)
