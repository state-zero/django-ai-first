import json
import time
import threading
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import override_settings, RequestFactory
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status
from pydantic import BaseModel

from django_ai.conversations.models import (
    ConversationSession,
    ConversationMessage,
)
from django_ai.conversations.base import ConversationAgent
from django_ai.conversations.registry import register_agent
from django_ai.conversations.decorators import with_context
from django_ai.conversations.context import (
    ResponseStream,
    display_widget,
)
from django_ai.conf import get_pusher_config
from django_ai.automation.workflows.core import engine
from django_ai.automation.queues.sync_executor import SynchronousExecutor

# Import crud to ensure models are registered with StateZero
from tests import crud

User = get_user_model()


class PusherEventCapture:
    """Captures real Pusher events by intercepting the trigger calls"""

    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def capture_trigger(self, original_trigger):
        """Create a wrapper that captures trigger calls"""

        def wrapper(channel, event, data):
            with self.lock:
                self.events.append(
                    {
                        "channel": channel,
                        "event": event,
                        "data": data,
                        "timestamp": time.time(),
                    }
                )
                print(f"[PUSHER] Event: {event} on {channel}")
            return original_trigger(channel, event, data)

        return wrapper

    def get_events_by_type(self, event_type):
        with self.lock:
            return [e for e in self.events if e["event"] == event_type]

    def clear_events(self):
        with self.lock:
            self.events.clear()


class FullFeatureTestAgent(ConversationAgent):
    """Test agent using the clean context system (sync-only)"""

    class Context(BaseModel):
        user_id: int = 0
        user_name: str = ""
        conversation_count: int = 0

    @classmethod
    def create_context(cls, request=None, **kwargs):
        """Create agent context from request and kwargs"""
        user = request.user if request and getattr(request.user, "is_authenticated", False) else None
        return cls.Context(
            user_id=user.id if user else 0,
            user_name=getattr(user, "username", "Anonymous") if user else "Anonymous",
            conversation_count=kwargs.get("conversation_count", 5),
        )

    @with_context()
    def get_user_info(self, user_id: int, user_name: str, conversation_count: int):
        """Agent function that uses context injection"""
        return {
            "id": user_id,
            "name": user_name,
            "total_conversations": conversation_count,
            "status": "active",
        }

    @with_context()
    def check_permissions(self, user_id: int, user_name: str):
        """Another context-injected function"""
        is_admin = user_name == "integrationtest"  # Our test user
        return f"User {user_name} ({'admin' if is_admin else 'regular'}) permissions verified"

    def get_response(self, message, request=None, files=None, **kwargs):
        """Main response handler with clean signature (sync)"""
        print(f"[AGENT] Processing: {message}")

        # Get agent context
        ctx = self.context

        # Parse the message to determine response type
        message_lower = message.lower()

        if "basic" in message_lower:
            # Basic non-streaming response
            return f"Basic response to: {message}"

        elif "stream" in message_lower:
            # “Streaming” response (sync) — still emits Pusher chunk events via ResponseStream
            return self._streaming_response(message)

        elif "user info" in message_lower:
            # Call agent function with context injection
            user_info = self.get_user_info()
            return f"User Information: {json.dumps(user_info, indent=2)}"

        elif "permissions" in message_lower:
            # Another agent function call
            perm_result = self.check_permissions()
            return f"Permission Check: {perm_result}"

        elif "widget" in message_lower:
            # Widget display
            display_widget(
                "conversation_widget",
                {
                    "title": "Conversation Progress",
                    "message": "Full conversation flow test",
                    "buttons": ["Continue", "Reset"],
                },
            )
            return "Interactive widget displayed above!"

        elif "file" in message_lower and files:
            # File processing - now uses explicit files parameter
            return self._process_files(files)

        else:
            # Default echo response with context update
            ctx.conversation_count += 1
            return f"Echo #{ctx.conversation_count}: {message}"

    def _streaming_response(self, original_message):
        """Generate a streaming-like response with multiple chunks (sync)"""
        print("[STREAM] Starting streaming response...")

        with ResponseStream() as stream:
            stream.write("Processing your streaming request")
            time.sleep(0.01)

            stream.write("... analyzing content")
            time.sleep(0.01)

            stream.write("... generating response")
            time.sleep(0.01)

            stream.write(
                f"... Complete! Your message '{original_message}' has been processed with streaming."
            )

        print(f"[OK] Streaming complete: {stream.content}")
        return stream.content

    def _process_files(self, files):
        """Process uploaded files - now receives files as parameter"""
        # File handling moved to django_ai.files module
        # Use django_ai.files.tools.get_file_content for new file handling
        return "File processing moved to django_ai.files module"


class FullConversationFlowTest(APITestCase):
    """Single comprehensive test covering the complete conversation flow"""

    def setUp(self):
        """Set up test environment"""
        engine.set_executor(SynchronousExecutor())

        # Create test user
        self.user = User.objects.create_user(
            username="integrationtest",
            password="testpass123",
            is_staff=True,
            email="integration@test.com",
        )
        self.client.force_authenticate(user=self.user)

        # Check Pusher configuration
        pusher_config = get_pusher_config()
        if not pusher_config.get("KEY"):
            self.skipTest(
                "DJANGO_AI['PUSHER'] not configured - skipping integration tests"
            )

        # Set up Pusher event capture
        self.pusher_capture = PusherEventCapture()

        # Register test agent
        register_agent("full_feature_agent", FullFeatureTestAgent)

        # Use the action to create session properly
        from django_ai.conversations.actions import start_conversation

        factory = RequestFactory()
        request = factory.post("/test")
        request.user = self.user

        result = start_conversation(
            agent_path="full_feature_agent",
            context_kwargs={"conversation_count": 5},
            request=request,
        )

        self.session = ConversationSession.objects.get(id=result["session_id"])

        print(f"[SETUP] Test Setup Complete - Session: {self.session.id}")

    def _send_message(self, content, files=None):
        """Send message via ConversationService with request context"""
        from django_ai.conversations.service import ConversationService

        # Create a mock request with our test user
        factory = RequestFactory()
        request = factory.post("/test")
        request.user = self.user

        return ConversationService.send_message(
            session_id=self.session.id,
            message=content,
            user=self.user,
            request=request,
            files=files,
        )

    def _verify_messages(self, expected_count):
        """Verify message count and return latest messages"""
        messages = ConversationMessage.objects.filter(session=self.session).order_by(
            "timestamp"
        )
        self.assertEqual(messages.count(), expected_count)
        return messages

    def test_complete_conversation_flow(self):
        """Test a complete conversation flow with all features"""

        with patch(
            "django_ai.conversations.context.pusher.Pusher"
        ) as mock_pusher_class:
            # Configure real Pusher with event capture
            import pusher

            pusher_config = get_pusher_config()
            real_pusher = pusher.Pusher(
                app_id=pusher_config.get("APP_ID"),
                key=pusher_config.get("KEY"),
                secret=pusher_config.get("SECRET"),
                cluster=pusher_config.get("CLUSTER", "us2"),
                ssl=True,
            )
            real_pusher.trigger = self.pusher_capture.capture_trigger(
                real_pusher.trigger
            )
            mock_pusher_class.return_value = real_pusher

            print(f"\n{'='*60}")
            print(f"[TEST] FULL CONVERSATION FLOW TEST")
            print(f"Session: {self.session.id}")
            print(f"{'='*60}")

            # Step 1: Basic Response (no streaming, no Pusher events expected)
            print(f"\n[STEP 1] Basic Response")
            self.pusher_capture.clear_events()

            result1 = self._send_message("Give me a basic response")
            print(f"   Result: {result1}")

            self.assertEqual(result1["status"], "success")
            self.assertEqual(
                result1["response"], "Basic response to: Give me a basic response"
            )

            messages = self._verify_messages(1)  # Only agent message
            print(f"   [OK] Messages created: {messages.count()}")
            print(
                f"   [OK] Pusher events (should be 0): {len(self.pusher_capture.events)}"
            )
            self.assertEqual(len(self.pusher_capture.events), 0)

            # Step 2: Streaming Response (should generate Pusher events)
            print(f"\n[STEP 2] Streaming Response")
            self.pusher_capture.clear_events()

            result2 = self._send_message("Please stream this response")
            print(f"   Result: {result2}")

            self.assertEqual(result2["status"], "success")
            self.assertIn("streaming", result2["response"].lower())
            self.assertIn("complete", result2["response"].lower())

            messages = self._verify_messages(2)  # 1 previous + 1 new
            print(f"   [OK] Messages created: {messages.count()}")
            print(
                f"   [OK] Pusher events (should be > 0): {len(self.pusher_capture.events)}"
            )
            self.assertGreater(len(self.pusher_capture.events), 0)

            # Check for streaming chunk events
            chunk_events = self.pusher_capture.get_events_by_type("text_chunk")
            print(f"   [OK] Streaming chunk events: {len(chunk_events)}")
            self.assertGreater(len(chunk_events), 0)

            # Step 3: Agent Function with Context Injection
            print(f"\n[STEP 3] Agent Function with Context")
            self.pusher_capture.clear_events()

            result3 = self._send_message("Show me user info")
            print(f"   Result: {result3}")

            self.assertEqual(result3["status"], "success")
            self.assertIn("User Information", result3["response"])
            self.assertIn("integrationtest", result3["response"])  # Our test username
            self.assertIn("total_conversations", result3["response"])

            messages = self._verify_messages(3)  # 2 previous + 1 new
            print(f"   [OK] Messages created: {messages.count()}")
            print(f"   [OK] Context injection worked: username found in response")

            # Step 4: Another Agent Function
            print(f"\n[STEP 4] Permission Check Function")
            self.pusher_capture.clear_events()

            result4 = self._send_message("Check my permissions")
            print(f"   Result: {result4}")

            self.assertEqual(result4["status"], "success")
            self.assertIn("Permission Check", result4["response"])
            self.assertIn("integrationtest", result4["response"])
            self.assertIn("admin", result4["response"])  # Should detect admin user

            messages = self._verify_messages(4)  # 3 previous + 1 new
            print(f"   [OK] Messages created: {messages.count()}")
            print(f"   [OK] Permission check worked")

            # Step 5: Widget Display
            print(f"\n[STEP 5] Widget Display")
            self.pusher_capture.clear_events()

            result5 = self._send_message("Show me a widget")
            print(f"   Result: {result5}")

            self.assertEqual(result5["status"], "success")
            self.assertEqual(result5["response"], "Interactive widget displayed above!")

            messages = self._verify_messages(5)  # 4 previous + 1 new
            print(f"   [OK] Messages created: {messages.count()}")
            print(f"   [OK] Widget display worked")

            # Step 6: Context Persistence Test
            print(f"\n[STEP 6] Context Persistence Test")
            self.pusher_capture.clear_events()

            result6 = self._send_message("Echo something")
            print(f"   Result: {result6}")

            self.assertEqual(result6["status"], "success")
            # Should show incremented conversation count
            self.assertIn("Echo #", result6["response"])

            messages = self._verify_messages(6)  # 5 previous + 1 new
            print(f"   [OK] Messages created: {messages.count()}")
            print(f"   [OK] Context persistence worked")

            # Step 7: Final Summary
            print(f"\n[STEP 7] Conversation Summary")

            final_messages = ConversationMessage.objects.filter(
                session=self.session
            ).order_by("timestamp")

            print(f"\n[SUMMARY] Final Conversation Summary:")
            print(f"   Total messages: {final_messages.count()}")
            print(f"   Session ID: {self.session.id}")

            # All messages should be agent responses
            for i, msg in enumerate(final_messages):
                print(f"   {i + 1}. {msg.message_type}: {msg.content[:60]}...")

            # Verify conversation flow
            self.assertEqual(len(final_messages), 6)

            # Verify each step worked correctly
            responses = [msg.content for msg in final_messages]
            self.assertTrue(any("Basic response" in r for r in responses))
            self.assertTrue(any("streaming" in r.lower() for r in responses))
            self.assertTrue(any("User Information" in r for r in responses))
            self.assertTrue(any("Permission Check" in r for r in responses))
            self.assertTrue(any("widget displayed" in r for r in responses))
            self.assertTrue(any("Echo #" in r for r in responses))

            print(f"\n{'='*60}")
            print(f"[DONE] FULL CONVERSATION FLOW TEST COMPLETE")
            print(f"[OK] All 6 conversation steps successful:")
            print(f"   1. Basic response [OK]")
            print(f"   2. Streaming response [OK]")
            print(f"   3. Context injection [OK]")
            print(f"   4. Agent functions [OK]")
            print(f"   5. Widget display [OK]")
            print(f"   6. Context persistence [OK]")
            print(f"[OK] Total messages: {final_messages.count()}")
            print(f"[OK] Clean context system working properly")
            print(f"[OK] Request passing working properly")
            print(f"{'='*60}")

# django_ai/conversations/tests.py (add this new test class)

class PusherAuthTest(APITestCase):
    """Test Pusher authentication endpoint"""

    def setUp(self):
        """Set up test environment"""
        # Create test users
        self.user1 = User.objects.create_user(
            username="user1",
            password="testpass123",
            email="user1@test.com",
        )
        self.user2 = User.objects.create_user(
            username="user2",
            password="testpass123",
            email="user2@test.com",
        )

        # Check Pusher configuration
        pusher_config = get_pusher_config()
        if not pusher_config.get("KEY"):
            self.skipTest(
                "DJANGO_AI['PUSHER'] not configured - skipping Pusher auth tests"
            )

        # Register a simple test agent
        register_agent("test_agent", FullFeatureTestAgent)

    def test_authenticated_user_can_access_own_session(self):
        """Test that authenticated users can only access their own conversation sessions"""
        # Create session for user1
        self.client.force_authenticate(user=self.user1)
        
        factory = RequestFactory()
        request = factory.post("/test")
        request.user = self.user1

        from django_ai.conversations.actions import start_conversation
        result = start_conversation(
            agent_path="test_agent",
            context_kwargs={},
            request=request,
        )
        
        session_id = result["session_id"]
        session = ConversationSession.objects.get(id=session_id)
        
        # User1 should be able to authenticate
        response = self.client.post(
            reverse('django_ai_conversations:pusher_auth'),
            {
                'socket_id': '123.456',
                'channel_name': f'private-conversation-session-{session_id}'
            },
            format='json'
        )
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('auth', response.data)
        print(f"[OK] User1 successfully authenticated to their own session")

    def test_authenticated_user_cannot_access_other_session(self):
        """Test that authenticated users cannot access other users' sessions"""
        # Create session for user1
        self.client.force_authenticate(user=self.user1)
        
        factory = RequestFactory()
        request = factory.post("/test")
        request.user = self.user1

        from django_ai.conversations.actions import start_conversation
        result = start_conversation(
            agent_path="test_agent",
            context_kwargs={},
            request=request,
        )
        
        session_id = result["session_id"]
        
        # Try to authenticate as user2
        self.client.force_authenticate(user=self.user2)
        
        response = self.client.post(
            reverse('django_ai_conversations:pusher_auth'),
            {
                'socket_id': '123.456',
                'channel_name': f'private-conversation-session-{session_id}'
            },
            format='json'
        )
        
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('error', response.data)
        print(f"[OK] User2 correctly denied access to User1's session")

    def test_anonymous_user_can_access_own_session(self):
        """Test that anonymous users can access their own sessions via anonymous_id"""
        # Create anonymous session
        anonymous_id = "anon_123456"
        
        session = ConversationSession.objects.create(
            agent_path="test_agent",
            anonymous_id=anonymous_id,
            context={}
        )
        
        # Simulate anonymous request with session
        session_data = self.client.session
        session_data['anonymous_id'] = anonymous_id
        session_data.save()
        
        response = self.client.post(
            reverse('django_ai_conversations:pusher_auth'),
            {
                'socket_id': '123.456',
                'channel_name': f'private-conversation-session-{session.id}',
                'anonymous_id': anonymous_id  # Also pass in request data as fallback
            },
            format='json'
        )
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('auth', response.data)
        print(f"[OK] Anonymous user successfully authenticated to their own session")

    def test_anonymous_user_cannot_access_other_session(self):
        """Test that anonymous users cannot access sessions with different anonymous_id"""
        # Create anonymous session
        session = ConversationSession.objects.create(
            agent_path="test_agent",
            anonymous_id="anon_123",
            context={}
        )
        
        # Try to access with different anonymous_id
        response = self.client.post(
            reverse('django_ai_conversations:pusher_auth'),
            {
                'socket_id': '123.456',
                'channel_name': f'private-conversation-session-{session.id}',
                'anonymous_id': 'anon_456'  # Different ID
            },
            format='json'
        )
        
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('error', response.data)
        print(f"[OK] Anonymous user correctly denied access to different session")

    def test_invalid_channel_name(self):
        """Test that invalid channel names are rejected"""
        self.client.force_authenticate(user=self.user1)
        
        response = self.client.post(
            reverse('django_ai_conversations:pusher_auth'),
            {
                'socket_id': '123.456',
                'channel_name': 'invalid-channel-name'
            },
            format='json'
        )
        
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('error', response.data)
        print(f"[OK] Invalid channel name correctly rejected")

    def test_missing_parameters(self):
        """Test that missing required parameters are rejected"""
        self.client.force_authenticate(user=self.user1)
        
        # Missing socket_id
        response = self.client.post(
            reverse('django_ai_conversations:pusher_auth'),
            {
                'channel_name': 'private-conversation-session-123'
            },
            format='json'
        )
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        
        # Missing channel_name
        response = self.client.post(
            reverse('django_ai_conversations:pusher_auth'),
            {
                'socket_id': '123.456'
            },
            format='json'
        )
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        print(f"[OK] Missing parameters correctly rejected")

    def test_nonexistent_session(self):
        """Test that non-existent sessions are rejected"""
        self.client.force_authenticate(user=self.user1)
        
        fake_session_id = "00000000-0000-0000-0000-000000000000"
        
        response = self.client.post(
            reverse('django_ai_conversations:pusher_auth'),
            {
                'socket_id': '123.456',
                'channel_name': f'private-conversation-session-{fake_session_id}'
            },
            format='json'
        )
        
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('error', response.data)
        print(f"[OK] Non-existent session correctly rejected")

    def test_pusher_auth_response_format(self):
        """Test that successful auth returns proper Pusher format"""
        # Create session for user1
        self.client.force_authenticate(user=self.user1)
        
        factory = RequestFactory()
        request = factory.post("/test")
        request.user = self.user1

        from django_ai.conversations.actions import start_conversation
        result = start_conversation(
            agent_path="test_agent",
            context_kwargs={},
            request=request,
        )
        
        session_id = result["session_id"]
        
        response = self.client.post(
            reverse('django_ai_conversations:pusher_auth'),
            {
                'socket_id': '123.456',
                'channel_name': f'private-conversation-session-{session_id}'
            },
            format='json'
        )
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Pusher auth response should have 'auth' key with signature
        self.assertIn('auth', response.data)
        
        # The auth value should be in format: "app_key:signature"
        auth_value = response.data['auth']
        self.assertIsInstance(auth_value, str)
        self.assertIn(':', auth_value)
        
        print(f"[OK] Pusher auth response format is correct")
        print(f"   Auth signature: {auth_value[:20]}...")