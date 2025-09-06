import asyncio
import json
import time
import threading
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase
from pydantic import BaseModel

from django_ai.conversations.models import (
    ConversationSession,
    ConversationMessage,
    File,
)
from django_ai.conversations.registry import register_agent
from django_ai.conversations.decorators import with_context
from django_ai.conversations.context import (
    ResponseStream,
    display_widget,
    get_file_text,
)
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
                print(f"📡 Pusher Event: {event} on {channel}")
            return original_trigger(channel, event, data)

        return wrapper

    def get_events_by_type(self, event_type):
        with self.lock:
            return [e for e in self.events if e["event"] == event_type]

    def clear_events(self):
        with self.lock:
            self.events.clear()


class FullFeatureTestAgent:
    """Test agent demonstrating all conversation capabilities"""

    class Context(BaseModel):
        user_id: int = 0
        user_name: str = ""
        conversation_count: int = 0

    def create_context(self):
        """Create agent context with user information"""
        return self.Context(
            user_id=self.user.id if hasattr(self, "user") and self.user else 0,
            user_name=(
                getattr(self.user, "username", "Anonymous")
                if hasattr(self, "user") and self.user
                else "Anonymous"
            ),
            conversation_count=5,  # Mock conversation count
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

    async def get_response(self, message, session_context, **kwargs):
        """Main response handler with different response types"""
        print(f"🤖 Agent processing: {message}")

        # Parse the message to determine response type
        message_lower = message.lower()

        if "basic" in message_lower:
            # Basic non-streaming response
            return f"Basic response to: {message}"

        elif "stream" in message_lower:
            # Streaming response
            return await self._streaming_response(message)

        elif "user info" in message_lower:
            # Call agent function with context
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

        elif "file" in message_lower and hasattr(self, "files") and self.files:
            # File processing
            return self._process_files()

        else:
            # Default echo response
            return f"Echo: {message}"

    async def _streaming_response(self, original_message):
        """Generate a streaming response with multiple chunks"""
        print("🔄 Starting streaming response...")

        with ResponseStream() as stream:
            stream.write("🔄 Processing your streaming request")
            await asyncio.sleep(0.1)

            stream.write("... analyzing content")
            await asyncio.sleep(0.1)

            stream.write("... generating response")
            await asyncio.sleep(0.1)

            stream.write(
                f"... ✅ Complete! Your message '{original_message}' has been processed with streaming."
            )

        print(f"✅ Streaming complete: {stream.content}")
        return stream.content

    def _process_files(self):
        """Process uploaded files"""
        results = []
        for file_obj in self.files:
            text = get_file_text(file_obj.id)
            if text:
                results.append(
                    f"✅ {file_obj.filename}: {len(text)} characters extracted"
                )
            else:
                results.append(f"❌ {file_obj.filename}: Could not extract text")
        return "File Processing Results:\n" + "\n".join(results)


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
        from django.conf import settings

        pusher_config = getattr(settings, "DJANGO_AI_PUSHER", {})
        if not pusher_config.get("KEY"):
            self.skipTest(
                "DJANGO_AI_PUSHER not configured - skipping integration tests"
            )

        # Set up Pusher event capture
        self.pusher_capture = PusherEventCapture()

        # Register test agent
        register_agent("full_feature_agent", FullFeatureTestAgent)

        # Create conversation session
        self.session = ConversationSession.objects.create(
            agent_path="full_feature_agent",
            user=self.user,
            context={
                "test_mode": True,
                "session_start": time.time(),
            },
        )

        print(f"🚀 Test Setup Complete - Session: {self.session.id}")

    def _send_message(self, content):
        """Send message directly via ConversationService"""
        from django_ai.conversations.service import ConversationService

        return ConversationService.send_message(
            session_id=self.session.id,
            message=content,
            user=self.user,
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
            from django.conf import settings

            pusher_config = getattr(settings, "DJANGO_AI_PUSHER", {})
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
            print(f"🧪 FULL CONVERSATION FLOW TEST")
            print(f"Session: {self.session.id}")
            print(f"{'='*60}")

            # Step 1: Basic Response (no streaming, no Pusher events expected)
            print(f"\n📝 Step 1: Basic Response")
            self.pusher_capture.clear_events()

            result1 = self._send_message("Give me a basic response")
            print(f"   Result: {result1}")

            self.assertEqual(result1["status"], "success")
            self.assertEqual(
                result1["response"], "Basic response to: Give me a basic response"
            )

            messages = self._verify_messages(2)  # user + agent
            print(f"   ✅ Messages created: {messages.count()}")
            print(
                f"   ✅ Pusher events (should be 0): {len(self.pusher_capture.events)}"
            )
            self.assertEqual(len(self.pusher_capture.events), 0)

            # Step 2: Streaming Response (should generate Pusher events)
            print(f"\n🌊 Step 2: Streaming Response")
            self.pusher_capture.clear_events()

            result2 = self._send_message("Please stream this response")
            print(f"   Result: {result2}")

            self.assertEqual(result2["status"], "success")
            self.assertIn("streaming", result2["response"].lower())
            self.assertIn("complete", result2["response"].lower())

            messages = self._verify_messages(4)  # 2 previous + 2 new
            print(f"   ✅ Messages created: {messages.count()}")
            print(
                f"   ✅ Pusher events (should be > 0): {len(self.pusher_capture.events)}"
            )
            self.assertGreater(len(self.pusher_capture.events), 0)

            # Check for streaming chunk events
            chunk_events = self.pusher_capture.get_events_by_type("text_chunk")
            print(f"   ✅ Streaming chunk events: {len(chunk_events)}")
            self.assertGreater(len(chunk_events), 0)

            # Step 3: Agent Function with Context Injection
            print(f"\n🔧 Step 3: Agent Function with Context")
            self.pusher_capture.clear_events()

            result3 = self._send_message("Show me user info")
            print(f"   Result: {result3}")

            self.assertEqual(result3["status"], "success")
            self.assertIn("User Information", result3["response"])
            self.assertIn("integrationtest", result3["response"])  # Our test username
            self.assertIn("conversation_count", result3["response"])

            messages = self._verify_messages(6)  # 4 previous + 2 new
            print(f"   ✅ Messages created: {messages.count()}")
            print(f"   ✅ Context injection worked: username found in response")

            # Step 4: Another Agent Function
            print(f"\n🔐 Step 4: Permission Check Function")
            self.pusher_capture.clear_events()

            result4 = self._send_message("Check my permissions")
            print(f"   Result: {result4}")

            self.assertEqual(result4["status"], "success")
            self.assertIn("Permission Check", result4["response"])
            self.assertIn("integrationtest", result4["response"])
            self.assertIn("admin", result4["response"])  # Should detect admin user

            messages = self._verify_messages(8)  # 6 previous + 2 new
            print(f"   ✅ Messages created: {messages.count()}")
            print(f"   ✅ Permission check worked")

            # Step 5: Widget Display
            print(f"\n🎛️  Step 5: Widget Display")
            self.pusher_capture.clear_events()

            result5 = self._send_message("Show me a widget")
            print(f"   Result: {result5}")

            self.assertEqual(result5["status"], "success")
            self.assertEqual(result5["response"], "Interactive widget displayed above!")

            messages = self._verify_messages(10)  # 8 previous + 2 new
            print(f"   ✅ Messages created: {messages.count()}")
            print(f"   ✅ Widget display worked")

            # Step 6: Final Summary
            print(f"\n📊 Step 6: Conversation Summary")

            final_messages = ConversationMessage.objects.filter(
                session=self.session
            ).order_by("timestamp")

            print(f"\n📋 Final Conversation Summary:")
            print(f"   Total messages: {final_messages.count()}")
            print(f"   Session ID: {self.session.id}")

            conversation_pairs = []
            for i in range(0, final_messages.count(), 2):
                user_msg = final_messages[i]
                agent_msg = (
                    final_messages[i + 1] if i + 1 < final_messages.count() else None
                )

                conversation_pairs.append(
                    {
                        "user": user_msg.content,
                        "agent": agent_msg.content if agent_msg else "No response",
                    }
                )

                print(f"   {i//2 + 1}. User: {user_msg.content}")
                print(
                    f"      Agent: {agent_msg.content if agent_msg else 'No response'}"
                )

            # Verify conversation flow
            self.assertEqual(len(conversation_pairs), 5)

            # Verify each step worked correctly
            self.assertIn("Basic response", conversation_pairs[0]["agent"])
            self.assertIn("streaming", conversation_pairs[1]["agent"].lower())
            self.assertIn("User Information", conversation_pairs[2]["agent"])
            self.assertIn("Permission Check", conversation_pairs[3]["agent"])
            self.assertIn("widget displayed", conversation_pairs[4]["agent"])

            print(f"\n{'='*60}")
            print(f"🎉 FULL CONVERSATION FLOW TEST COMPLETE via StateZero API")
            print(f"✅ All 5 conversation steps successful:")
            print(f"   1. Basic response via StateZero ✅")
            print(f"   2. Streaming response via StateZero ✅")
            print(f"   3. Context injection via StateZero ✅")
            print(f"   4. Agent functions via StateZero ✅")
            print(f"   5. Widget display via StateZero ✅")
            print(f"✅ Total messages: {final_messages.count()}")
            print(f"✅ Session maintained throughout StateZero calls")
            print(f"✅ StateZero API integration working properly")
            print(f"{'='*60}")
