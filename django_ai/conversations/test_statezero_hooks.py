import time
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITransactionTestCase
from rest_framework import status
from pydantic import BaseModel

from django_ai.conversations.models import (
    ConversationSession,
    ConversationMessage,
)
from django_ai.conversations.base import ConversationAgent
from django_ai.conversations.registry import register_agent
from django_ai.automation.workflows.core import engine
from django_ai.automation.queues.sync_executor import SynchronousExecutor

# Import crud to ensure models are registered with StateZero
from tests import crud

User = get_user_model()


class SimpleTestAgent(ConversationAgent):
    """Simple test agent that echoes messages"""
    
    class Context(BaseModel):
        message_count: int = 0
    
    @classmethod
    def create_context(cls, request=None, **kwargs):
        return cls.Context(message_count=kwargs.get("message_count", 0))
    
    def get_response(self, message, request=None, files=None, **kwargs):
        # Increment message count in context
        self.context.message_count += 1
        return f"Echo #{self.context.message_count}: {message}"


class MessagingFlowHappyPathTest(APITransactionTestCase):
    """Simple happy path test for the complete messaging flow via StateZero"""

    def setUp(self):
        """Set up test environment"""
        # Set up synchronous executor for immediate task processing
        engine.set_executor(SynchronousExecutor())
        
        # Create test user
        self.user = User.objects.create_user(
            username="testuser",
            password="testpass123",
            email="test@example.com",
        )
        self.client.force_authenticate(user=self.user)

        # Register test agent
        register_agent("simple_agent", SimpleTestAgent)

        # Create a session
        self.session = ConversationSession.objects.create(
            agent_path="simple_agent",
            user=self.user,
            context={"message_count": 0},
        )

        print(f"\n{'='*60}")
        print(f"🚀 Messaging Flow Happy Path Test")
        print(f"   Session: {self.session.id}")
        print(f"   User: {self.user.username}")
        print(f"{'='*60}\n")

    def wait_for_processing(self, message_id, timeout=5):
        """Wait for a message to be processed"""
        start = time.time()
        while time.time() - start < timeout:
            message = ConversationMessage.objects.get(id=message_id)
            if message.processing_status == "completed":
                return message
            time.sleep(0.1)  # Poll every 100ms
        
        # Timeout - raise assertion error
        message = ConversationMessage.objects.get(id=message_id)
        self.fail(f"Message processing timed out. Status: {message.processing_status}")

    def test_complete_messaging_flow(self):
        """Test the complete happy path: create message -> hooks run -> agent responds"""
        
        # Step 1: Create a user message via StateZero
        print("📝 Step 1: Creating user message via StateZero...")
        
        url = reverse('statezero:model_view', kwargs={'model_name': 'conversations.ConversationMessage'})
        
        response = self.client.post(
            url,
            {
                'ast': {
                    'query': {
                        'type': 'create',
                        'data': {
                            'session': str(self.session.id),
                            'content': 'Hello, agent!',
                        }
                    },
                    'serializerOptions': {}
                }
            },
            format='json'
        )
        
        print(f"   Response status: {response.status_code}")
        
        # Verify request was successful
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Extract ID from nested response
        user_message_id = response.data['data']['data'][0]
        self.assertIsNotNone(user_message_id, "Response should contain message ID")
        
        user_message = ConversationMessage.objects.get(id=user_message_id)
        print(f"   ✅ User message created: {user_message.id}")
        print(f"      Content: {user_message.content}")
        print(f"      Type: {user_message.message_type}")
        print(f"      Timestamp: {user_message.timestamp}")
        print(f"      Initial status: {user_message.processing_status}")
        
        # Verify pre-hooks worked
        self.assertEqual(user_message.message_type, "user")
        
        # Wait for processing to complete
        print(f"   ⏳ Waiting for processing to complete...")
        user_message = self.wait_for_processing(user_message_id)
        print(f"   ✅ Processing completed")
        
        self.assertEqual(user_message.processing_status, "completed")
        
        # Step 2: Verify agent response was created
        print(f"\n🤖 Step 2: Checking for agent response...")
        
        agent_messages = ConversationMessage.objects.filter(
            session=self.session,
            message_type="agent"
        ).order_by('timestamp')
        
        print(f"   Agent messages found: {agent_messages.count()}")
        
        self.assertEqual(agent_messages.count(), 1, "Should have exactly one agent response")
        
        agent_message = agent_messages.first()
        print(f"   ✅ Agent response created: {agent_message.id}")
        print(f"      Content: {agent_message.content}")
        print(f"      Type: {agent_message.message_type}")
        print(f"      Timestamp: {agent_message.timestamp}")
        
        # Verify agent response content
        self.assertEqual(agent_message.message_type, "agent")
        self.assertIn("Echo #1", agent_message.content)
        self.assertIn("Hello, agent!", agent_message.content)
        
        # Step 3: Verify complete conversation and message ordering
        print(f"\n📊 Step 3: Verifying complete conversation and ordering...")
        
        all_messages = ConversationMessage.objects.filter(
            session=self.session
        ).order_by('timestamp')
        
        print(f"   Total messages: {all_messages.count()}")
        
        self.assertEqual(all_messages.count(), 2, "Should have user message + agent response")
        
        messages_list = list(all_messages)
        self.assertEqual(messages_list[0].message_type, "user")
        self.assertEqual(messages_list[1].message_type, "agent")
        
        # VERIFY TIMESTAMP ORDERING
        print(f"\n   📅 Timestamp verification:")
        print(f"      User message timestamp:  {messages_list[0].timestamp}")
        print(f"      Agent message timestamp: {messages_list[1].timestamp}")
        
        self.assertLess(
            messages_list[0].timestamp,
            messages_list[1].timestamp,
            "Agent message should have a later timestamp than user message"
        )
        
        time_diff = (messages_list[1].timestamp - messages_list[0].timestamp).total_seconds()
        print(f"      Time difference: {time_diff:.3f} seconds")
        print(f"      ✅ Timestamps are correctly ordered")
        
        print(f"\n   Conversation history:")
        for i, msg in enumerate(messages_list, 1):
            print(f"   {i}. [{msg.message_type.upper()}] {msg.content}")
        
        # Step 4: Send another message to verify context persistence and ordering
        print(f"\n📝 Step 4: Testing context persistence with second message...")
        
        response2 = self.client.post(
            url,
            {
                'ast': {
                    'query': {
                        'type': 'create',
                        'data': {
                            'session': str(self.session.id),
                            'content': 'Second message!',
                        }
                    },
                    'serializerOptions': {}
                }
            },
            format='json'
        )
        
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        
        # Get the second message ID and wait for processing
        user_message_2_id = response2.data['data']['data'][0]
        print(f"   ⏳ Waiting for second message processing...")
        user_message_2 = self.wait_for_processing(user_message_2_id)
        print(f"   ✅ Second message processed")
        print(f"      Timestamp: {user_message_2.timestamp}")
        
        # Check agent responses
        agent_messages = ConversationMessage.objects.filter(
            session=self.session,
            message_type="agent"
        ).order_by('timestamp')
        
        self.assertEqual(agent_messages.count(), 2, "Should have two agent responses now")
        
        second_response = agent_messages.last()
        print(f"   ✅ Second agent response: {second_response.content}")
        print(f"      Timestamp: {second_response.timestamp}")
        
        # Verify context was preserved (message count incremented)
        self.assertIn("Echo #2", second_response.content)
        self.assertIn("Second message!", second_response.content)
        
        # Verify ordering of all messages
        print(f"\n   📅 Complete conversation timestamp verification:")
        all_messages = ConversationMessage.objects.filter(
            session=self.session
        ).order_by('timestamp')
        
        messages_list = list(all_messages)
        self.assertEqual(len(messages_list), 4, "Should have 4 total messages")
        
        # Verify each message comes after the previous one
        for i in range(len(messages_list) - 1):
            print(f"      Message {i+1} ({messages_list[i].message_type}): {messages_list[i].timestamp}")
            self.assertLess(
                messages_list[i].timestamp,
                messages_list[i+1].timestamp,
                f"Message {i+2} should have a later timestamp than message {i+1}"
            )
        
        print(f"      Message {len(messages_list)} ({messages_list[-1].message_type}): {messages_list[-1].timestamp}")
        print(f"      ✅ All timestamps are correctly ordered")
        
        # Final summary
        final_count = ConversationMessage.objects.filter(session=self.session).count()
        print(f"\n{'='*60}")
        print(f"🎉 HAPPY PATH TEST COMPLETE!")
        print(f"{'='*60}")
        print(f"✅ User message created via StateZero")
        print(f"✅ Pre-hooks set message_type and processing_status")
        print(f"✅ Signal triggered message processing after commit")
        print(f"✅ Agent processed message and responded")
        print(f"✅ Agent response saved to database")
        print(f"✅ Context persisted across messages")
        print(f"✅ Timestamps are correctly ordered")
        print(f"   Total messages in conversation: {final_count}")
        print(f"{'='*60}\n")