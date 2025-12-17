from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from ..events.models import Event, EventStatus
from ..events.definitions import EventDefinition, EventTrigger
from tests.models import TestBooking, TestOrder, TestProperty, TestModelWithoutEvents, TestUUIDModel, TestTriggerModel, TestWatchFieldsModel, Guest, Booking

class EventDefinitionTest(TestCase):
    """Test the EventDefinition helper class functionality"""

    def test_event_definition_creation_with_date_field(self):
        """Test EventDefinition creation for scheduled events"""
        event_def = EventDefinition(
            "test_event",
            date_field="checkin_date",
            condition=lambda i: i.status == "confirmed",
        )

        self.assertEqual(event_def.name, "test_event")
        self.assertEqual(event_def.date_field, "checkin_date")
        self.assertFalse(event_def.is_immediate())

        # Test condition evaluation
        booking = TestBooking(status="confirmed")
        self.assertTrue(event_def.should_create(booking))

        booking.status = "pending"
        self.assertFalse(event_def.should_create(booking))

    def test_event_definition_immediate_event(self):
        """Test EventDefinition creation for immediate events"""
        event_def = EventDefinition(
            "immediate_event", condition=lambda i: i.status == "confirmed"
        )

        self.assertEqual(event_def.name, "immediate_event")
        self.assertIsNone(event_def.date_field)
        self.assertTrue(event_def.is_immediate())

    def test_event_definition_no_condition(self):
        """Test EventDefinition with no condition (always True)"""
        event_def = EventDefinition("always_event", date_field="checkin_date")

        booking = TestBooking(status="anything")
        self.assertTrue(event_def.should_create(booking))

    def test_get_date_value_scheduled(self):
        """Test extracting date value from model instance for scheduled events"""
        event_def = EventDefinition("test_event", date_field="checkin_date")
        test_date = timezone.now()
        booking = TestBooking(checkin_date=test_date)

        self.assertEqual(event_def.get_date_value(booking), test_date)

    def test_get_date_value_immediate(self):
        """Test date value for immediate events returns None"""
        event_def = EventDefinition("immediate_event")
        booking = TestBooking()

        self.assertIsNone(event_def.get_date_value(booking))


class EventModelTest(TransactionTestCase):
    """Test the Event model functionality and properties"""

    def setUp(self):
        """Create test models for use in event tests"""
        self.test_date = timezone.now() + timedelta(days=1)
        self.booking = TestBooking.objects.create(
            guest_name="John Doe",
            checkin_date=self.test_date,
            checkout_date=self.test_date + timedelta(days=2),
            status="confirmed",
        )

        self.order = TestOrder.objects.create(total=1500.00, status="confirmed")

    def test_event_creation_on_save(self):
        """Test that events are automatically created when model is saved"""
        # Test booking events
        booking_events = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(self.booking.id),
        )

        # Should have created 5 events (4 scheduled + 1 immediate)
        self.assertEqual(booking_events.count(), 5)

        event_names = set(booking_events.values_list("event_name", flat=True))
        expected_names = {
            "checkin_due",
            "checkout_due",
            "review_request",
            "always_event",
            "booking_confirmed",
        }
        self.assertEqual(event_names, expected_names)

        # Test order events
        order_events = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestOrder),
            entity_id=str(self.order.id),
        )

        # Should have created 2 immediate events
        self.assertEqual(order_events.count(), 2)

        order_event_names = set(order_events.values_list("event_name", flat=True))
        expected_order_names = {"order_placed", "high_value_alert"}
        self.assertEqual(order_event_names, expected_order_names)

    def test_event_entity_property(self):
        """Test that event.entity returns the correct model instance"""
        event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(self.booking.id),
            event_name="checkin_due",
        ).first()

        entity = event.entity
        self.assertEqual(entity.id, self.booking.id)
        self.assertEqual(entity.guest_name, self.booking.guest_name)

    def test_event_at_property_scheduled(self):
        """Test that event.at returns the correct date for scheduled events"""
        event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(self.booking.id),
            event_name="checkin_due",
        ).first()

        # event.at should pull from the entity's checkin_date field
        self.assertEqual(event.at, self.booking.checkin_date)
        self.assertFalse(event.is_immediate)

    def test_event_at_property_immediate(self):
        """Test that event.at returns creation time for immediate events"""
        event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestOrder),
            entity_id=str(self.order.id),
            event_name="order_placed",
        ).first()

        # event.at should be the creation time for immediate events
        self.assertEqual(event.at, event.event_created_at)
        self.assertTrue(event.is_immediate)

    def test_event_is_valid_property(self):
        """Test that event.is_valid correctly evaluates conditions at runtime"""
        # Get events for confirmed booking
        checkin_event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(self.booking.id),
            event_name="checkin_due",
        ).first()

        review_event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(self.booking.id),
            event_name="review_request",
        ).first()

        booking_confirmed_event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(self.booking.id),
            event_name="booking_confirmed",
        ).first()

        # checkin_due should be valid (status='confirmed' meets condition)
        self.assertTrue(checkin_event.is_valid)

        # review_request should be invalid (status needs to be 'completed')
        self.assertFalse(review_event.is_valid)

        # booking_confirmed (immediate) should be valid (status='confirmed')
        self.assertTrue(booking_confirmed_event.is_valid)

    def test_event_validity_changes_with_status(self):
        """Test that event validity changes dynamically when model status changes"""
        review_event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(self.booking.id),
            event_name="review_request",
        ).first()

        # Initially invalid (status='confirmed', needs 'completed')
        self.assertFalse(review_event.is_valid)

        # Change status to completed
        self.booking.status = "completed"
        self.booking.save()

        # Now should be valid (condition now met)
        self.assertTrue(review_event.is_valid)

    def test_event_deletion_on_model_delete(self):
        """Test that events are automatically deleted when model is deleted"""
        booking_id = self.booking.id

        # Verify events exist before deletion
        events_count = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(booking_id),
        ).count()
        self.assertGreater(events_count, 0)

        # Delete the booking
        self.booking.delete()

        # Verify all events are deleted via signal
        events_count = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(booking_id),
        ).count()
        self.assertEqual(events_count, 0)

    def test_no_events_for_model_without_events_attribute(self):
        """Test that no events are created for models without events attribute"""
        model_without_events = TestModelWithoutEvents.objects.create(name="Test")

        events_count = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestModelWithoutEvents),
            entity_id=str(model_without_events.id),
        ).count()

        # Should be 0 since TestModelWithoutEvents has no 'events' attribute
        self.assertEqual(events_count, 0)


class EventManagerTest(TransactionTestCase):
    """Test the EventManager query methods"""

    def setUp(self):
        """Create test models with different dates and statuses"""
        self.now = timezone.now()
        self.today = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.tomorrow = self.today + timedelta(days=1)
        self.next_week = self.today + timedelta(days=7)

        # Create bookings with different dates and statuses
        self.booking_today = TestBooking.objects.create(
            guest_name="Today Guest",
            checkin_date=self.today + timedelta(hours=12),
            checkout_date=self.today + timedelta(days=1),
            status="confirmed",
        )

        self.booking_tomorrow = TestBooking.objects.create(
            guest_name="Tomorrow Guest",
            checkin_date=self.tomorrow + timedelta(hours=12),
            checkout_date=self.tomorrow + timedelta(days=1),
            status="confirmed",
        )

        self.booking_cancelled = TestBooking.objects.create(
            guest_name="Cancelled Guest",
            checkin_date=self.today + timedelta(hours=12),
            checkout_date=self.today + timedelta(days=1),
            status="cancelled",  # This makes conditional events invalid
        )

        # Create orders for immediate event testing
        self.order_high_value = TestOrder.objects.create(
            total=2000.00, status="confirmed"
        )

        self.order_low_value = TestOrder.objects.create(total=50.00, status="pending")

    def test_get_events_date_range(self):
        """Test getting events within a specific date range"""
        events = Event.objects.get_events(
            from_date=self.today,
            to_date=self.tomorrow + timedelta(hours=23, minutes=59),
        )

        # Expected scheduled events within range:
        # - Today Guest: checkin_due, checkout_due, always_event (3 valid)
        # - Tomorrow Guest: checkin_due, always_event (2 valid, checkout_due is day after tomorrow)
        # - Cancelled Guest: always_event only (1 valid, conditional events invalid)
        # Total scheduled events: 6
        # Plus immediate events that are included by default
        self.assertGreaterEqual(len(events), 6)

    def test_get_events_scheduled_only(self):
        """Test getting only scheduled events in date range"""
        scheduled_events = Event.objects.get_due_events(
            from_date=self.today,
            to_date=self.tomorrow + timedelta(hours=23, minutes=59),
        )

        # Should not include immediate events
        for event in scheduled_events:
            self.assertFalse(event.is_immediate)
            self.assertIsNotNone(event.at)

    def test_get_events_with_status_filter(self):
        # Baseline counts before we flip one scheduled event
        baseline_pending = len(Event.objects.get_events(
            from_date=self.today,
            to_date=self.next_week + timedelta(days=1),
            status=EventStatus.PENDING,
        ))
        baseline_processed = len(Event.objects.get_events(
            from_date=self.today,
            to_date=self.next_week + timedelta(days=1),
            status=EventStatus.PROCESSED,
        ))

        # Flip a scheduled (non-immediate) event to processed
        event = Event.objects.filter(
            event_name="checkin_due",
            status=EventStatus.PENDING,
        ).first()
        event.status = EventStatus.PROCESSED
        event.save()

        pending_events = Event.objects.get_events(
            from_date=self.today,
            to_date=self.next_week + timedelta(days=1),
            status=EventStatus.PENDING,
        )
        processed_events = Event.objects.get_events(
            from_date=self.today,
            to_date=self.next_week + timedelta(days=1),
            status=EventStatus.PROCESSED,
        )

        # Verify the filter works: one fewer pending, one more processed
        self.assertEqual(len(pending_events), baseline_pending - 1)
        self.assertEqual(len(processed_events), baseline_processed + 1)

    def test_get_events_only_returns_valid_events(self):
        """Test that get_events only returns events that are currently valid"""
        events = Event.objects.get_events(
            from_date=self.today,
            to_date=self.tomorrow + timedelta(hours=23, minutes=59),
        )

        # All returned events should pass their validity conditions
        for event in events:
            self.assertTrue(event.is_valid, f"Event {event.event_name} should be valid")

    def test_get_events_empty_range(self):
        """Test getting events with a date range that contains no events"""
        future_date = self.now + timedelta(days=365)
        events = Event.objects.get_due_events(
            from_date=future_date, to_date=future_date + timedelta(days=1)
        )

        # Should return no events for far future date range
        self.assertEqual(len(events), 0)

    def test_get_events_no_date_range(self):
        """Test getting all events without date range"""
        all_events = Event.objects.get_events()

        # Should include both immediate and scheduled events
        immediate_events = [e for e in all_events if e.is_immediate]
        scheduled_events = [e for e in all_events if not e.is_immediate]

        self.assertGreater(len(immediate_events), 0)
        self.assertGreater(len(scheduled_events), 0)

    def test_update_for_instance(self):
        """Test manual event creation for a model instance"""
        property_instance = TestProperty.objects.create(
            name="Test Property", maintenance_due=self.tomorrow, status="active"
        )

        # Verify event was automatically created via signal
        events = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestProperty),
            entity_id=str(property_instance.id),
        )
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().event_name, "maintenance_due")


class EventStringRepresentationTest(TransactionTestCase):
    """Test string representation and display of events"""

    def test_event_str_method_scheduled(self):
        """Test the string representation of scheduled Event objects"""
        booking = TestBooking.objects.create(
            guest_name="Test Guest",
            checkin_date=timezone.now(),
            checkout_date=timezone.now() + timedelta(days=1),
            status="confirmed",
        )

        event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(booking.id),
            event_name="checkin_due",
        ).first()

        str_repr = str(event)
        # Should contain event name, time, and model info
        self.assertIn("checkin_due", str_repr)
        self.assertIn("testbooking", str_repr.lower())
        self.assertIn(str(booking.id), str_repr)

    def test_event_str_method_immediate(self):
        """Test the string representation of immediate Event objects"""
        order = TestOrder.objects.create(total=1500.00, status="confirmed")

        event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestOrder),
            entity_id=str(order.id),
            event_name="order_placed",
        ).first()

        str_repr = str(event)
        # Should contain event name and creation time
        self.assertIn("order_placed", str_repr)
        self.assertIn("testorder", str_repr.lower())


class EventErrorHandlingTest(TransactionTestCase):
    """Test error handling and edge cases"""

    def test_event_with_deleted_entity(self):
        """Test event behavior when referenced entity no longer exists"""
        booking = TestBooking.objects.create(
            guest_name="Test Guest",
            checkin_date=timezone.now(),
            checkout_date=timezone.now() + timedelta(days=1),
            status="confirmed",
        )

        event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(booking.id),
            event_name="checkin_due",
        ).first()

        # Delete the booking (this should also delete events via signal)
        booking.delete()

        # Create an orphaned event to test graceful error handling
        orphaned_event = Event.objects.create(
            event_name="orphaned_event",
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id="999999",  # Non-existent ID
        )

        # These should not raise exceptions but return None/False gracefully
        self.assertIsNone(orphaned_event.at)
        self.assertFalse(orphaned_event.is_valid)
        self.assertIsNone(orphaned_event._get_event_definition())

    def test_immediate_event_conditions(self):
        """Test that immediate events respect their conditions"""
        # Order with low value should not trigger high_value_alert
        low_value_order = TestOrder.objects.create(total=50.00, status="confirmed")

        events = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestOrder),
            entity_id=str(low_value_order.id),
        )

        # Should have order_placed but not high_value_alert
        event_names = set(events.values_list("event_name", flat=True))
        self.assertIn("order_placed", event_names)

        # Check validity
        for event in events:
            if event.event_name == "order_placed":
                self.assertTrue(event.is_valid)
            elif event.event_name == "high_value_alert":
                self.assertFalse(
                    event.is_valid
                )  # Total is only 50, condition is > 1000


class EventNamespaceTest(TransactionTestCase):
    """Test namespace functionality for events and callbacks"""

    def setUp(self):
        from django_ai.automation.events.callbacks import callback_registry

        callback_registry.clear()

    def tearDown(self):
        from django_ai.automation.events.callbacks import callback_registry

        callback_registry.clear()

    def test_event_creation_with_custom_namespace(self):
        """Test that events can be created with custom namespaces"""
        # Create a booking - should use default namespace "*"
        booking = TestBooking.objects.create(
            guest_name="Test Guest",
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="confirmed",
        )

        event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(booking.id),
            event_name="booking_confirmed",
        )

        # Default namespace should be "*"
        self.assertEqual(event.namespace, "*")

    def test_callback_namespace_filtering(self):
        """Test that callbacks are filtered by namespace"""
        from django_ai.automation.events.callbacks import callback_registry, on_event

        callback_results = []

        @on_event(event_name="booking_confirmed", namespace="tenant_a")
        def tenant_a_callback(event):
            callback_results.append(f"tenant_a_{event.id}")

        @on_event(event_name="booking_confirmed", namespace="tenant_b")
        def tenant_b_callback(event):
            callback_results.append(f"tenant_b_{event.id}")

        @on_event(event_name="booking_confirmed", namespace="*")
        def wildcard_callback(event):
            callback_results.append(f"wildcard_{event.id}")

        # Create event with tenant_a namespace
        event_a = Event.objects.create(
            event_name="booking_confirmed",
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id="123",
            namespace="tenant_a",
        )

        # Create event with tenant_b namespace
        event_b = Event.objects.create(
            event_name="booking_confirmed",
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id="456",
            namespace="tenant_b",
        )

        # Trigger callbacks
        event_a.mark_as_occurred()
        event_b.mark_as_occurred()

        # Check results
        self.assertIn(f"tenant_a_{event_a.id}", callback_results)
        self.assertIn(
            f"wildcard_{event_a.id}", callback_results
        )  # Wildcard should match
        self.assertNotIn(f"tenant_b_{event_a.id}", callback_results)  # Wrong namespace

        self.assertIn(f"tenant_b_{event_b.id}", callback_results)
        self.assertIn(
            f"wildcard_{event_b.id}", callback_results
        )  # Wildcard should match
        self.assertNotIn(f"tenant_a_{event_b.id}", callback_results)  # Wrong namespace

    def test_wildcard_namespace_matches_all(self):
        """Test that wildcard namespace "*" matches all events"""
        from django_ai.automation.events.callbacks import callback_registry, on_event

        callback_results = []

        @on_event(event_name="booking_confirmed", namespace="*")
        def wildcard_callback(event):
            callback_results.append(f"wildcard_{event.namespace}_{event.id}")

        # Create events with different namespaces
        namespaces = ["tenant_a", "tenant_b", "custom_namespace", "*"]
        events = []

        for ns in namespaces:
            event = Event.objects.create(
                event_name="booking_confirmed",
                model_type=ContentType.objects.get_for_model(TestBooking),
                entity_id=f"test_{ns}",
                namespace=ns,
            )
            events.append(event)
            event.mark_as_occurred()

        # Wildcard callback should have been called for all events
        for event in events:
            self.assertIn(f"wildcard_{event.namespace}_{event.id}", callback_results)


class EventPrimaryKeyTypeCastingTest(TransactionTestCase):
    """Test that events work correctly with different primary key types.

    The Event model stores entity_id as a CharField (varchar), but when querying
    related entities, we need to cast the entity_id to match the target model's
    PK type (e.g., bigint for AutoField, uuid for UUIDField).
    """

    def test_events_with_integer_pk(self):
        """Test that events work with standard integer (AutoField) primary keys"""
        now = timezone.now()
        booking = TestBooking.objects.create(
            guest_name="Integer PK Test",
            checkin_date=now + timedelta(days=1),
            checkout_date=now + timedelta(days=2),
            status="confirmed",
        )

        # Verify events were created
        events = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(booking.id),
        )
        self.assertGreater(events.count(), 0)

        # Test get_events query with date range (uses Cast internally)
        due_events = Event.objects.get_events(
            from_date=now,
            to_date=now + timedelta(days=3),
        )

        # Should include at least one event from our booking
        booking_events = [e for e in due_events if e.entity_id == str(booking.id)]
        self.assertGreater(len(booking_events), 0)

        # Verify entity is correctly fetched
        for event in booking_events:
            self.assertEqual(event.entity.id, booking.id)
            self.assertEqual(event.entity.guest_name, "Integer PK Test")

    def test_events_with_uuid_pk(self):
        """Test that events work with UUID primary keys"""
        now = timezone.now()
        uuid_model = TestUUIDModel.objects.create(
            name="UUID PK Test",
            due_date=now + timedelta(days=1),
            active=True,
        )

        # Verify events were created with UUID as entity_id
        events = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestUUIDModel),
            entity_id=str(uuid_model.id),
        )
        self.assertEqual(events.count(), 2)  # scheduled + immediate

        event_names = set(events.values_list("event_name", flat=True))
        self.assertEqual(event_names, {"uuid_scheduled_event", "uuid_immediate_event"})

        # Test get_events query with date range (uses Cast internally for UUID)
        due_events = Event.objects.get_events(
            from_date=now,
            to_date=now + timedelta(days=3),
        )

        # Should include events from our UUID model
        uuid_events = [e for e in due_events if e.entity_id == str(uuid_model.id)]
        self.assertGreater(len(uuid_events), 0)

        # Verify entity is correctly fetched via UUID
        for event in uuid_events:
            self.assertEqual(event.entity.id, uuid_model.id)
            self.assertEqual(event.entity.name, "UUID PK Test")

    def test_mixed_pk_types_in_single_query(self):
        """Test that get_events handles multiple models with different PK types"""
        now = timezone.now()

        # Create entity with integer PK
        booking = TestBooking.objects.create(
            guest_name="Mixed Test Booking",
            checkin_date=now + timedelta(days=1),
            checkout_date=now + timedelta(days=2),
            status="confirmed",
        )

        # Create entity with UUID PK
        uuid_model = TestUUIDModel.objects.create(
            name="Mixed Test UUID",
            due_date=now + timedelta(days=1),
            active=True,
        )

        # Query events - this should handle both integer and UUID PKs
        due_events = Event.objects.get_events(
            from_date=now,
            to_date=now + timedelta(days=3),
        )

        # Find events for both models
        booking_events = [e for e in due_events if e.entity_id == str(booking.id)]
        uuid_events = [e for e in due_events if e.entity_id == str(uuid_model.id)]

        # Both should have events returned
        self.assertGreater(len(booking_events), 0, "Should have events for integer PK model")
        self.assertGreater(len(uuid_events), 0, "Should have events for UUID PK model")

        # Verify entities are correctly resolved
        self.assertEqual(booking_events[0].entity.guest_name, "Mixed Test Booking")
        self.assertEqual(uuid_events[0].entity.name, "Mixed Test UUID")


class EventOffsetCallbackTest(TransactionTestCase):
    """Test that callbacks with offsets fire at the correct time."""

    def setUp(self):
        from django_ai.automation.events.callbacks import callback_registry
        callback_registry.clear()

    def tearDown(self):
        from django_ai.automation.events.callbacks import callback_registry
        callback_registry.clear()

    def test_callback_with_negative_offset_fires_before_event_time(self):
        """Callback with negative offset should fire before event.at time."""
        from django_ai.automation.events.callbacks import callback_registry, on_event
        from django_ai.automation.events.services import event_processor
        from django_ai.automation.testing import time_machine

        callback_log = []

        @on_event(event_name="checkin_due", offset=timedelta(minutes=-60))
        def pre_checkin_callback(event):
            callback_log.append(f"pre_checkin:{event.entity_id}")

        @on_event(event_name="checkin_due")
        def at_checkin_callback(event):
            callback_log.append(f"at_checkin:{event.entity_id}")

        with time_machine() as tm:
            # Create booking with checkin 3 hours from now
            now = timezone.now()
            checkin_time = now + timedelta(hours=3)

            booking = TestBooking.objects.create(
                guest_name="Offset Test",
                checkin_date=checkin_time,
                checkout_date=checkin_time + timedelta(days=1),
                status="confirmed",
            )

            # At creation time, no callbacks should have fired yet
            self.assertEqual(callback_log, [])

            # Advance to 1 hour before checkin (2 hours from start)
            # This is when the -60 offset callback should fire
            tm.advance(hours=2, minutes=1)

            self.assertIn(f"pre_checkin:{booking.id}", callback_log)
            self.assertNotIn(f"at_checkin:{booking.id}", callback_log)

            # Advance to checkin time
            tm.advance(hours=1)

            self.assertIn(f"at_checkin:{booking.id}", callback_log)

    def test_callback_with_positive_offset_fires_after_event_time(self):
        """Callback with positive offset should fire after event.at time."""
        from django_ai.automation.events.callbacks import callback_registry, on_event
        from django_ai.automation.testing import time_machine

        callback_log = []

        @on_event(event_name="checkout_due", offset=timedelta(minutes=60))
        def post_checkout_callback(event):
            callback_log.append(f"post_checkout:{event.entity_id}")

        @on_event(event_name="checkout_due")
        def at_checkout_callback(event):
            callback_log.append(f"at_checkout:{event.entity_id}")

        with time_machine() as tm:
            now = timezone.now()
            checkout_time = now + timedelta(hours=2)

            booking = TestBooking.objects.create(
                guest_name="Positive Offset Test",
                checkin_date=now - timedelta(days=1),
                checkout_date=checkout_time,
                status="confirmed",
            )

            # No callbacks yet
            self.assertEqual(callback_log, [])

            # Advance to checkout time
            tm.advance(hours=2, minutes=1)

            self.assertIn(f"at_checkout:{booking.id}", callback_log)
            self.assertNotIn(f"post_checkout:{booking.id}", callback_log)

            # Advance 1 more hour (past the +60 offset)
            tm.advance(hours=1)

            self.assertIn(f"post_checkout:{booking.id}", callback_log)

    def test_multiple_offsets_same_event(self):
        """Multiple callbacks with different offsets on same event fire at correct times."""
        from django_ai.automation.events.callbacks import callback_registry, on_event
        from django_ai.automation.testing import time_machine

        callback_log = []

        @on_event(event_name="checkin_due", offset=timedelta(minutes=-120))
        def two_hours_before(event):
            callback_log.append("2h_before")

        @on_event(event_name="checkin_due", offset=timedelta(minutes=-60))
        def one_hour_before(event):
            callback_log.append("1h_before")

        @on_event(event_name="checkin_due")
        def at_checkin(event):
            callback_log.append("at_checkin")

        @on_event(event_name="checkin_due", offset=timedelta(minutes=60))
        def one_hour_after(event):
            callback_log.append("1h_after")

        with time_machine() as tm:
            now = timezone.now()
            checkin_time = now + timedelta(hours=4)

            TestBooking.objects.create(
                guest_name="Multi Offset Test",
                checkin_date=checkin_time,
                checkout_date=checkin_time + timedelta(days=1),
                status="confirmed",
            )

            # Advance to 2 hours before checkin
            tm.advance(hours=2, minutes=1)
            self.assertIn("2h_before", callback_log)
            self.assertNotIn("1h_before", callback_log)

            # Advance to 1 hour before checkin
            tm.advance(hours=1)
            self.assertIn("1h_before", callback_log)
            self.assertNotIn("at_checkin", callback_log)

            # Advance to checkin time
            tm.advance(hours=1)
            self.assertIn("at_checkin", callback_log)
            self.assertNotIn("1h_after", callback_log)

            # Advance to 1 hour after checkin
            tm.advance(hours=1)
            self.assertIn("1h_after", callback_log)

    def test_offset_callbacks_only_fire_once(self):
        """Callbacks with offsets should only fire once, not on every poll cycle."""
        from django_ai.automation.events.callbacks import callback_registry, on_event
        from django_ai.automation.testing import time_machine

        callback_counts = {"2h_before": 0, "1h_before": 0, "at_checkin": 0, "1h_after": 0}

        @on_event(event_name="checkin_due", offset=timedelta(minutes=-120))
        def two_hours_before(event):
            callback_counts["2h_before"] += 1

        @on_event(event_name="checkin_due", offset=timedelta(minutes=-60))
        def one_hour_before(event):
            callback_counts["1h_before"] += 1

        @on_event(event_name="checkin_due")
        def at_checkin(event):
            callback_counts["at_checkin"] += 1

        @on_event(event_name="checkin_due", offset=timedelta(minutes=60))
        def one_hour_after(event):
            callback_counts["1h_after"] += 1

        with time_machine() as tm:
            now = timezone.now()
            checkin_time = now + timedelta(hours=4)

            TestBooking.objects.create(
                guest_name="Idempotency Test",
                checkin_date=checkin_time,
                checkout_date=checkin_time + timedelta(days=1),
                status="confirmed",
            )

            # Advance to 2 hours before checkin and poll multiple times
            tm.advance(hours=2, minutes=1)
            self.assertEqual(callback_counts["2h_before"], 1)

            # Process again - should NOT increment
            tm.process()
            tm.process()
            tm.process()
            self.assertEqual(callback_counts["2h_before"], 1, "2h_before callback fired multiple times!")

            # Advance to 1 hour before checkin
            tm.advance(hours=1)
            self.assertEqual(callback_counts["1h_before"], 1)

            # Process again - should NOT increment
            tm.process()
            tm.process()
            self.assertEqual(callback_counts["1h_before"], 1, "1h_before callback fired multiple times!")
            # 2h_before should still be 1
            self.assertEqual(callback_counts["2h_before"], 1)

            # Advance to checkin time
            tm.advance(hours=1)
            self.assertEqual(callback_counts["at_checkin"], 1)

            # Process again - should NOT increment
            tm.process()
            tm.process()
            self.assertEqual(callback_counts["at_checkin"], 1, "at_checkin callback fired multiple times!")

            # Advance to 1 hour after checkin
            tm.advance(hours=1)
            self.assertEqual(callback_counts["1h_after"], 1)

            # Process again - should NOT increment
            tm.process()
            tm.process()
            self.assertEqual(callback_counts["1h_after"], 1, "1h_after callback fired multiple times!")

            # Final check - each callback should have fired exactly once
            self.assertEqual(callback_counts, {
                "2h_before": 1,
                "1h_before": 1,
                "at_checkin": 1,
                "1h_after": 1,
            })

    def test_relativedelta_offset_works(self):
        """Callbacks with relativedelta offsets should work correctly.

        This test verifies that relativedelta can be used as an offset type,
        which is useful for calendar-aware durations like 'months' or 'years'.
        """
        from django_ai.automation.events.callbacks import callback_registry, on_event
        from django_ai.automation.testing import time_machine

        callback_log = []

        # Use relativedelta for calendar-aware offset (7 days before, using weeks)
        @on_event(event_name="checkin_due", offset=relativedelta(weeks=-1))
        def one_week_before(event):
            callback_log.append("1_week_before")

        @on_event(event_name="checkin_due")
        def at_checkin(event):
            callback_log.append("at_checkin")

        with time_machine() as tm:
            now = timezone.now()
            # Checkin is 2 weeks from now
            checkin_time = now + timedelta(weeks=2)

            TestBooking.objects.create(
                guest_name="RelativeDelta Test",
                checkin_date=checkin_time,
                checkout_date=checkin_time + timedelta(days=1),
                status="confirmed",
            )

            # Advance to 1 week before checkin (should trigger 1_week_before)
            # relativedelta(weeks=-1) means fire 7 days before event.at
            tm.advance(days=8)  # 1 week + 1 day = 8 days
            self.assertIn("1_week_before", callback_log)
            self.assertNotIn("at_checkin", callback_log)

            # Advance to checkin time
            tm.advance(days=7)  # Another week
            self.assertIn("at_checkin", callback_log)

    def test_comprehensive_offset_timing_with_event_changes(self):
        """
        Comprehensive test of offset timing with scheduled events.

        Tests:
        - Multiple offsets (before, at, after) fire at correct times
        - Callbacks don't fire before their scheduled time
        - Changing event times doesn't re-fire already-processed offsets
        - New events with same offsets work independently
        """
        from django_ai.automation.events.callbacks import callback_registry, on_event
        from django_ai.automation.events.models import ProcessedEventOffset
        from django_ai.automation.testing import time_machine

        # Track callback invocations with timestamps
        callback_log = []

        @on_event(event_name="checkin_due", offset=timedelta(hours=-2))
        def two_hours_before(event):
            callback_log.append(("2h_before", event.entity_id, timezone.now()))

        @on_event(event_name="checkin_due", offset=timedelta(hours=-1))
        def one_hour_before(event):
            callback_log.append(("1h_before", event.entity_id, timezone.now()))

        @on_event(event_name="checkin_due")
        def at_checkin(event):
            callback_log.append(("at_checkin", event.entity_id, timezone.now()))

        @on_event(event_name="checkin_due", offset=timedelta(minutes=30))
        def thirty_min_after(event):
            callback_log.append(("30m_after", event.entity_id, timezone.now()))

        @on_event(event_name="checkin_due", offset=timedelta(hours=1))
        def one_hour_after(event):
            callback_log.append(("1h_after", event.entity_id, timezone.now()))

        with time_machine() as tm:
            start_time = timezone.now()

            # Create a booking with checkin 4 hours from now
            checkin_time = start_time + timedelta(hours=4)
            booking = TestBooking.objects.create(
                guest_name="Timing Test",
                checkin_date=checkin_time,
                checkout_date=checkin_time + timedelta(days=1),
                status="confirmed",
            )
            booking_id = str(booking.id)

            # === Test 1: Nothing should fire yet (we're 4 hours before checkin) ===
            self.assertEqual(len(callback_log), 0, "No callbacks should fire yet")

            # === Test 2: Advance to 2h before checkin - only 2h_before should fire ===
            tm.advance(hours=2, minutes=1)  # Now 1h59m before checkin

            callbacks_fired = [c[0] for c in callback_log if c[1] == booking_id]
            self.assertEqual(callbacks_fired, ["2h_before"],
                f"Only 2h_before should fire. Got: {callbacks_fired}")

            # === Test 3: Advance to 1h before - 1h_before should fire ===
            tm.advance(hours=1)  # Now 59m before checkin

            callbacks_fired = [c[0] for c in callback_log if c[1] == booking_id]
            self.assertEqual(callbacks_fired, ["2h_before", "1h_before"],
                f"2h_before and 1h_before should have fired. Got: {callbacks_fired}")

            # === Test 4: Advance to checkin time - at_checkin should fire ===
            tm.advance(hours=1)  # Now at checkin time

            callbacks_fired = [c[0] for c in callback_log if c[1] == booking_id]
            self.assertEqual(callbacks_fired, ["2h_before", "1h_before", "at_checkin"],
                f"at_checkin should have fired. Got: {callbacks_fired}")

            # === Test 5: Advance 30 min after - 30m_after should fire ===
            tm.advance(minutes=31)

            callbacks_fired = [c[0] for c in callback_log if c[1] == booking_id]
            self.assertIn("30m_after", callbacks_fired,
                f"30m_after should have fired. Got: {callbacks_fired}")

            # === Test 6: Advance 1 hour after - 1h_after should fire ===
            tm.advance(minutes=30)  # Now 1h1m after checkin

            callbacks_fired = [c[0] for c in callback_log if c[1] == booking_id]
            self.assertEqual(
                callbacks_fired,
                ["2h_before", "1h_before", "at_checkin", "30m_after", "1h_after"],
                f"All callbacks should have fired in order. Got: {callbacks_fired}"
            )

            # === Test 7: Change the event time - callbacks should NOT re-fire ===
            # Move checkin to 2 hours later
            new_checkin = checkin_time + timedelta(hours=2)
            booking.checkin_date = new_checkin
            booking.save()

            # Process again - nothing new should fire
            tm.process()
            tm.process()

            callbacks_for_booking = [c for c in callback_log if c[1] == booking_id]
            self.assertEqual(len(callbacks_for_booking), 5,
                "Changing event time should NOT re-fire callbacks")

            # === Test 8: Create a NEW booking - it should get its own callbacks ===
            callback_log.clear()  # Clear for clarity

            new_checkin_time = timezone.now() + timedelta(hours=3)
            booking2 = TestBooking.objects.create(
                guest_name="Second Booking",
                checkin_date=new_checkin_time,
                checkout_date=new_checkin_time + timedelta(days=1),
                status="confirmed",
            )
            booking2_id = str(booking2.id)

            # Advance to 1h before new booking's checkin
            tm.advance(hours=2, minutes=1)

            callbacks_for_booking2 = [c[0] for c in callback_log if c[1] == booking2_id]
            self.assertIn("2h_before", callbacks_for_booking2)
            self.assertIn("1h_before", callbacks_for_booking2)

            # Original booking should have no new callbacks
            callbacks_for_booking1 = [c[0] for c in callback_log if c[1] == booking_id]
            self.assertEqual(len(callbacks_for_booking1), 0,
                "Original booking should not get new callbacks")

            # === Test 9: Verify ProcessedEventOffset records exist ===
            # This ensures the idempotency tracking is working
            from django_ai.automation.events.models import Event
            event1 = Event.objects.get(entity_id=booking_id, event_name="checkin_due")
            processed_offsets = ProcessedEventOffset.objects.filter(event=event1)

            # Should have 4 offset records (excluding zero offset which uses event status)
            offset_values = set(p.offset for p in processed_offsets)
            self.assertIn(timedelta(hours=-2), offset_values)
            self.assertIn(timedelta(hours=-1), offset_values)
            self.assertIn(timedelta(minutes=30), offset_values)
            self.assertIn(timedelta(hours=1), offset_values)

    def test_immediate_event_with_positive_offsets(self):
        """
        Test offset callbacks with immediate events.

        Immediate events have event.at = created_at, so:
        - Negative offsets don't make sense (fire before creation)
        - Positive offsets fire X time after creation (e.g., follow-up actions)
        """
        from django_ai.automation.events.callbacks import callback_registry, on_event
        from django_ai.automation.events.models import ProcessedEventOffset
        from django_ai.automation.testing import time_machine
        from tests.models import TestOrder

        callback_log = []

        @on_event(event_name="order_placed")
        def on_order_placed(event):
            callback_log.append(("immediate", event.entity_id, timezone.now()))

        @on_event(event_name="order_placed", offset=timedelta(minutes=30))
        def thirty_min_followup(event):
            callback_log.append(("30m_followup", event.entity_id, timezone.now()))

        @on_event(event_name="order_placed", offset=timedelta(hours=1))
        def one_hour_followup(event):
            callback_log.append(("1h_followup", event.entity_id, timezone.now()))

        with time_machine() as tm:
            # Create an order - this is an immediate event
            order = TestOrder.objects.create(
                total=500,
                status="confirmed",
            )
            order_id = str(order.id)

            # Immediate callback should fire right away (on next process)
            callbacks_fired = [c[0] for c in callback_log if c[1] == order_id]
            self.assertEqual(callbacks_fired, ["immediate"],
                f"Immediate callback should fire on creation. Got: {callbacks_fired}")

            # === Test: Advance 30 minutes - 30m_followup should fire ===
            tm.advance(minutes=31)

            callbacks_fired = [c[0] for c in callback_log if c[1] == order_id]
            self.assertEqual(callbacks_fired, ["immediate", "30m_followup"],
                f"30m_followup should fire after 30 minutes. Got: {callbacks_fired}")

            # === Test: Advance to 1 hour - 1h_followup should fire ===
            tm.advance(minutes=30)

            callbacks_fired = [c[0] for c in callback_log if c[1] == order_id]
            self.assertEqual(callbacks_fired, ["immediate", "30m_followup", "1h_followup"],
                f"1h_followup should fire after 1 hour. Got: {callbacks_fired}")

            # === Test: Process again - nothing should re-fire ===
            tm.process()
            tm.process()

            callbacks_fired = [c[0] for c in callback_log if c[1] == order_id]
            self.assertEqual(len(callbacks_fired), 3,
                "Callbacks should not re-fire on repeated processing")

            # === Test: Verify ProcessedEventOffset records ===
            from django_ai.automation.events.models import Event
            event = Event.objects.get(entity_id=order_id, event_name="order_placed")
            processed_offsets = ProcessedEventOffset.objects.filter(event=event)

            offset_values = set(p.offset for p in processed_offsets)
            self.assertIn(timedelta(minutes=30), offset_values)
            self.assertIn(timedelta(hours=1), offset_values)


class EventTriggerTest(TransactionTestCase):
    """Test EventTrigger functionality (CREATE, UPDATE, DELETE)"""

    def setUp(self):
        from django_ai.automation.events.callbacks import callback_registry
        callback_registry.clear()

    def tearDown(self):
        from django_ai.automation.events.callbacks import callback_registry
        callback_registry.clear()

    def test_event_definition_trigger_default(self):
        """Test that EventDefinition defaults to CREATE trigger"""
        event_def = EventDefinition("test_event")
        self.assertEqual(event_def.triggers, [EventTrigger.CREATE])

    def test_event_definition_trigger_single(self):
        """Test EventDefinition with single trigger"""
        event_def = EventDefinition("test_event", trigger=EventTrigger.UPDATE)
        self.assertEqual(event_def.triggers, [EventTrigger.UPDATE])

    def test_event_definition_trigger_list(self):
        """Test EventDefinition with list of triggers"""
        event_def = EventDefinition(
            "test_event",
            trigger=[EventTrigger.CREATE, EventTrigger.UPDATE]
        )
        self.assertEqual(event_def.triggers, [EventTrigger.CREATE, EventTrigger.UPDATE])

    def test_event_definition_trigger_invalid(self):
        """Test EventDefinition raises error for invalid trigger"""
        with self.assertRaises(ValueError):
            EventDefinition("test_event", trigger="invalid")

        with self.assertRaises(ValueError):
            EventDefinition("test_event", trigger=["invalid"])

    def test_should_trigger_method(self):
        """Test the should_trigger method"""
        create_only = EventDefinition("test", trigger=EventTrigger.CREATE)
        update_only = EventDefinition("test", trigger=EventTrigger.UPDATE)
        both = EventDefinition("test", trigger=[EventTrigger.CREATE, EventTrigger.UPDATE])

        self.assertTrue(create_only.should_trigger(EventTrigger.CREATE))
        self.assertFalse(create_only.should_trigger(EventTrigger.UPDATE))

        self.assertFalse(update_only.should_trigger(EventTrigger.CREATE))
        self.assertTrue(update_only.should_trigger(EventTrigger.UPDATE))

        self.assertTrue(both.should_trigger(EventTrigger.CREATE))
        self.assertTrue(both.should_trigger(EventTrigger.UPDATE))

    def test_create_trigger_fires_only_on_creation(self):
        """Test that CREATE trigger only fires on initial save"""
        from django_ai.automation.events.callbacks import on_event

        callback_log = []

        @on_event(event_name="entity_created")
        def on_created(event):
            callback_log.append(f"created:{event.entity_id}")

        # Create instance - should fire
        instance = TestTriggerModel.objects.create(name="Test")

        self.assertEqual(len(callback_log), 1)
        self.assertIn(f"created:{instance.id}", callback_log)

        # Update instance - should NOT fire again
        callback_log.clear()
        instance.name = "Updated"
        instance.save()

        self.assertEqual(len(callback_log), 0, "CREATE trigger should not fire on update")

    def test_update_trigger_fires_only_on_updates(self):
        """Test that UPDATE trigger only fires on subsequent saves"""
        from django_ai.automation.events.callbacks import on_event

        callback_log = []

        @on_event(event_name="entity_modified")
        def on_modified(event):
            callback_log.append(f"modified:{event.entity_id}")

        # Create instance - UPDATE trigger should NOT fire
        instance = TestTriggerModel.objects.create(name="Test")
        self.assertEqual(len(callback_log), 0, "UPDATE trigger should not fire on creation")

        # Update instance - should fire
        instance.name = "Updated"
        instance.save()

        self.assertEqual(len(callback_log), 1)
        self.assertIn(f"modified:{instance.id}", callback_log)

        # Update again - should fire again (repeatable)
        callback_log.clear()
        instance.name = "Updated Again"
        instance.save()

        self.assertEqual(len(callback_log), 1, "UPDATE trigger should fire on each update")

    def test_combined_triggers_fire_on_both(self):
        """Test that combined triggers fire on both create and update"""
        from django_ai.automation.events.callbacks import on_event

        callback_log = []

        @on_event(event_name="entity_saved")
        def on_saved(event):
            callback_log.append(f"saved:{event.entity_id}")

        # Create instance - should fire
        instance = TestTriggerModel.objects.create(name="Test")
        self.assertEqual(len(callback_log), 1)

        # Update instance - should fire again
        instance.name = "Updated"
        instance.save()

        self.assertEqual(len(callback_log), 2, "Combined trigger should fire on both create and update")

    def test_delete_trigger_fires_on_deletion(self):
        """Test that DELETE trigger fires when instance is deleted"""
        from django_ai.automation.events.callbacks import on_event

        callback_log = []

        @on_event(event_name="entity_deleted")
        def on_deleted(event):
            callback_log.append(f"deleted:{event.entity_id}")

        # Create instance
        instance = TestTriggerModel.objects.create(name="Test")
        instance_id = instance.id

        self.assertEqual(len(callback_log), 0, "DELETE trigger should not fire on creation")

        # Delete instance - should fire
        instance.delete()

        self.assertEqual(len(callback_log), 1)
        self.assertIn(f"deleted:{instance_id}", callback_log)

    def test_update_trigger_respects_condition(self):
        """Test that UPDATE trigger respects condition on each update"""
        from django_ai.automation.events.callbacks import on_event

        callback_log = []

        @on_event(event_name="entity_modified")
        def on_modified(event):
            callback_log.append(f"modified:{event.entity_id}")

        # Create with active status
        instance = TestTriggerModel.objects.create(name="Test", status="active")

        # Update while active - should fire
        instance.name = "Updated"
        instance.save()
        self.assertEqual(len(callback_log), 1)

        # Change to inactive status - should NOT fire (condition fails)
        callback_log.clear()
        instance.status = "inactive"
        instance.save()
        self.assertEqual(len(callback_log), 0, "UPDATE trigger should not fire when condition fails")

        # Change back to active and update - should fire
        instance.status = "active"
        instance.name = "Active Again"
        instance.save()
        self.assertEqual(len(callback_log), 1)

    def test_backward_compatibility_default_trigger(self):
        """Test that existing events without trigger param work (default to CREATE)"""
        # TestBooking uses old-style EventDefinitions without trigger param
        booking = TestBooking.objects.create(
            guest_name="Test Guest",
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="confirmed",
        )

        # Events should be created
        events = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(booking.id),
        )
        self.assertGreater(events.count(), 0)

        # Immediate event should have fired (booking_confirmed)
        booking_confirmed = events.filter(event_name="booking_confirmed").first()
        self.assertIsNotNone(booking_confirmed)
        self.assertEqual(booking_confirmed.status, EventStatus.PROCESSED)


class EventClaimTest(TransactionTestCase):
    """Test the event claims system for flow control."""

    def setUp(self):
        from django_ai.automation.events.models import EventClaim
        EventClaim.objects.all().delete()

        self.booking = TestBooking.objects.create(
            guest_name="Claim Test Guest",
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="confirmed",
        )

    def test_claim_creation_requires_context(self):
        """Test that claims.hold() requires execution context"""
        from django_ai.automation.events import claims

        with self.assertRaises(RuntimeError) as ctx:
            claims.hold("booking_confirmed", match={"guest_name": "Test"})

        self.assertIn("must be called from within a handler", str(ctx.exception))

    def test_claim_creation_with_context(self):
        """Test creating a claim within execution context"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent:
            pass

        with claims._context("agent", MockAgent, 123):
            result = claims.hold("booking_confirmed", match={"guest_name": "Test Guest"})

        # No bumped claim, so result should be None
        self.assertIsNone(result)

        # Verify claim was created
        claim = EventClaim.objects.get(event_type="booking_confirmed")
        self.assertEqual(claim.owner_type, "agent")
        self.assertEqual(claim.owner_class, "MockAgent")
        self.assertEqual(claim.owner_run_id, 123)
        self.assertEqual(claim.match, {"guest_name": "Test Guest"})

    def test_claim_with_time_expiry(self):
        """Test claim with custom time-based expiry"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent:
            pass

        with claims._context("agent", MockAgent, 123):
            claims.hold(
                "order_placed",
                match={"status": "confirmed"},
                expires_in=timedelta(hours=2)
            )

        claim = EventClaim.objects.get(event_type="order_placed")
        self.assertIsNotNone(claim.expires_at)

        # Verify expiry is approximately 2 hours from now
        expected = timezone.now() + timedelta(hours=2)
        self.assertAlmostEqual(
            claim.expires_at.timestamp(),
            expected.timestamp(),
            delta=5  # 5 second tolerance
        )

    def test_claim_with_event_count_expiry(self):
        """Test claim with event count expiry"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent:
            pass

        with claims._context("agent", MockAgent, 123):
            claims.hold(
                "order_placed",
                match={"status": "confirmed"},
                expires_after_events=5
            )

        claim = EventClaim.objects.get(event_type="order_placed")
        self.assertEqual(claim.max_events, 5)
        self.assertEqual(claim.events_handled, 0)

    def test_claim_is_expired_by_time(self):
        """Test that is_expired property works for time expiry"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent:
            pass

        with claims._context("agent", MockAgent, 123):
            claims.hold(
                "order_placed",
                match={"status": "confirmed"},
                expires_in=timedelta(seconds=-1)  # Already expired
            )

        claim = EventClaim.objects.get(event_type="order_placed")
        self.assertTrue(claim.is_expired)

    def test_claim_is_expired_by_event_count(self):
        """Test that is_expired property works for event count expiry"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent:
            pass

        with claims._context("agent", MockAgent, 123):
            claims.hold(
                "order_placed",
                match={"status": "confirmed"},
                expires_in=None,  # No time expiry
                expires_after_events=2
            )

        claim = EventClaim.objects.get(event_type="order_placed")
        self.assertFalse(claim.is_expired)

        # Increment to max
        claim.increment_events()
        claim.increment_events()

        self.assertTrue(claim.is_expired)

    def test_claim_release(self):
        """Test releasing a claim"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent:
            pass

        with claims._context("agent", MockAgent, 123):
            claims.hold("order_placed", match={"status": "confirmed"})
            self.assertEqual(EventClaim.objects.count(), 1)

            result = claims.release("order_placed", match={"status": "confirmed"})
            self.assertTrue(result)
            self.assertEqual(EventClaim.objects.count(), 0)

    def test_release_nonexistent_claim(self):
        """Test releasing a claim that doesn't exist"""
        from django_ai.automation.events import claims

        class MockAgent:
            pass

        with claims._context("agent", MockAgent, 123):
            result = claims.release("order_placed", match={"status": "nonexistent"})
            self.assertFalse(result)

    def test_cannot_release_other_owners_claim(self):
        """Test that you cannot release another owner's claim"""
        from django_ai.automation.events import claims

        class MockAgent1:
            pass

        class MockAgent2:
            pass

        # Agent 1 creates claim
        with claims._context("agent", MockAgent1, 100):
            claims.hold("order_placed", match={"status": "confirmed"})

        # Agent 2 tries to release it
        with claims._context("agent", MockAgent2, 200):
            with self.assertRaises(ValueError) as ctx:
                claims.release("order_placed", match={"status": "confirmed"})

            self.assertIn("owned by MockAgent1", str(ctx.exception))

    def test_release_all_for_owner(self):
        """Test releasing all claims for an owner"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent:
            pass

        with claims._context("agent", MockAgent, 123):
            claims.hold("order_placed", match={"status": "confirmed"})
            claims.hold("order_placed", match={"status": "pending"})
            claims.hold("booking_confirmed", match={"guest_name": "Test"})

        self.assertEqual(EventClaim.objects.count(), 3)

        count = claims.release_all_for_owner("agent", 123)
        self.assertEqual(count, 3)
        self.assertEqual(EventClaim.objects.count(), 0)

    def test_find_matching_claim(self):
        """Test finding a claim that matches an entity"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent:
            pass

        with claims._context("agent", MockAgent, 123):
            claims.hold("booking_confirmed", match={"guest_name": "Claim Test Guest"})

        # Find claim for our booking
        matching_claim = claims.find_matching_claim("booking_confirmed", self.booking)
        self.assertIsNotNone(matching_claim)
        self.assertEqual(matching_claim.owner_class, "MockAgent")

    def test_find_matching_claim_no_match(self):
        """Test that find_matching_claim returns None when no claim matches"""
        from django_ai.automation.events import claims

        class MockAgent:
            pass

        with claims._context("agent", MockAgent, 123):
            claims.hold("booking_confirmed", match={"guest_name": "Other Guest"})

        # Our booking has guest_name="Claim Test Guest", not "Other Guest"
        matching_claim = claims.find_matching_claim("booking_confirmed", self.booking)
        self.assertIsNone(matching_claim)

    def test_holder_function(self):
        """Test the holder() function returns ClaimInfo"""
        from django_ai.automation.events import claims

        class MockAgent:
            pass

        with claims._context("agent", MockAgent, 123):
            claims.hold("booking_confirmed", match={"guest_name": "Claim Test Guest"})

        holder_info = claims.holder("booking_confirmed", self.booking)
        self.assertIsNotNone(holder_info)
        self.assertEqual(holder_info.owner_class, "MockAgent")
        self.assertEqual(holder_info.owner_run_id, 123)
        self.assertEqual(holder_info.event_type, "booking_confirmed")

    def test_priority_based_claim_resolution(self):
        """Test that higher priority claims win"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent1:
            pass

        class MockAgent2:
            pass

        # Agent 1 creates claim with priority 10
        with claims._context("agent", MockAgent1, 100):
            claims.hold("booking_confirmed", match={"guest_name": "Claim Test Guest"}, priority=10)

        # Agent 2 tries to create claim with lower priority - should fail
        with claims._context("agent", MockAgent2, 200):
            with self.assertRaises(ValueError) as ctx:
                claims.hold("booking_confirmed", match={"guest_name": "Claim Test Guest"}, priority=5)

            self.assertIn("priority", str(ctx.exception).lower())

        # Agent 2 with higher priority can take over
        with claims._context("agent", MockAgent2, 200):
            bumped = claims.hold("booking_confirmed", match={"guest_name": "Claim Test Guest"}, priority=20)

        # Should have bumped the original claim
        self.assertIsNotNone(bumped)
        self.assertEqual(bumped.owner_class, "MockAgent1")
        self.assertEqual(bumped.owner_run_id, 100)

        # New claim should be from Agent2
        claim = EventClaim.objects.get(event_type="booking_confirmed")
        self.assertEqual(claim.owner_class, "MockAgent2")
        self.assertEqual(claim.owner_run_id, 200)

    def test_bump_flag_forces_takeover(self):
        """Test that bump=True forces claim takeover regardless of priority"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent1:
            pass

        class MockAgent2:
            pass

        # Agent 1 creates claim with high priority
        with claims._context("agent", MockAgent1, 100):
            claims.hold("booking_confirmed", match={"guest_name": "Claim Test Guest"}, priority=100)

        # Agent 2 with bump=True can take over despite lower priority
        with claims._context("agent", MockAgent2, 200):
            bumped = claims.hold(
                "booking_confirmed",
                match={"guest_name": "Claim Test Guest"},
                priority=1,
                bump=True
            )

        self.assertIsNotNone(bumped)
        self.assertEqual(bumped.owner_class, "MockAgent1")

        claim = EventClaim.objects.get(event_type="booking_confirmed")
        self.assertEqual(claim.owner_class, "MockAgent2")

    def test_claim_refresh_by_same_owner(self):
        """Test that same owner can refresh their claim"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent:
            pass

        with claims._context("agent", MockAgent, 123):
            claims.hold(
                "booking_confirmed",
                match={"guest_name": "Claim Test Guest"},
                priority=5,
                expires_in=timedelta(hours=1)
            )

            original_expires = EventClaim.objects.get(event_type="booking_confirmed").expires_at

            # Same owner refreshes claim with new settings
            result = claims.hold(
                "booking_confirmed",
                match={"guest_name": "Claim Test Guest"},
                priority=10,
                expires_in=timedelta(hours=2)
            )

            # Should return None (no bump, just refresh)
            self.assertIsNone(result)

            # Claim should be updated
            claim = EventClaim.objects.get(event_type="booking_confirmed")
            self.assertEqual(claim.priority, 10)
            self.assertGreater(claim.expires_at, original_expires)

    def test_validate_match_catches_invalid_filter(self):
        """Test that invalid match dict is caught at claim time"""
        from django_ai.automation.events import claims

        class MockAgent:
            pass

        with claims._context("agent", MockAgent, 123):
            with self.assertRaises(ValueError) as ctx:
                claims.hold(
                    "booking_confirmed",
                    match={"nonexistent_field__icontains": "test"}
                )

            self.assertIn("Invalid match filter", str(ctx.exception))

    def test_expired_claim_is_replaced(self):
        """Test that expired claims are automatically replaced"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent1:
            pass

        class MockAgent2:
            pass

        # Agent 1 creates expired claim
        with claims._context("agent", MockAgent1, 100):
            claims.hold(
                "booking_confirmed",
                match={"guest_name": "Claim Test Guest"},
                expires_in=timedelta(seconds=-10)  # Already expired
            )

        # Agent 2 should be able to take over without bump
        with claims._context("agent", MockAgent2, 200):
            result = claims.hold(
                "booking_confirmed",
                match={"guest_name": "Claim Test Guest"},
                priority=1
            )

        # Should not report a bump (expired claim was just deleted)
        self.assertIsNone(result)

        # Claim should be from Agent2
        claim = EventClaim.objects.get(event_type="booking_confirmed")
        self.assertEqual(claim.owner_class, "MockAgent2")

    def test_holder_returns_none_when_no_claim(self):
        """Test holder() returns None when no claim exists"""
        from django_ai.automation.events import claims

        holder_info = claims.holder("booking_confirmed", self.booking)
        self.assertIsNone(holder_info)

    def test_claim_info_is_expired_property(self):
        """Test ClaimInfo.is_expired property"""
        from django_ai.automation.events.claims import ClaimInfo
        from django_ai.automation.events.models import EventClaim

        # Create a claim that will expire soon
        claim = EventClaim.objects.create(
            event_type="booking_confirmed",
            match={"guest_name": "Test"},
            owner_type="agent",
            owner_class="TestAgent",
            owner_run_id=123,
            expires_at=timezone.now() - timedelta(seconds=10),  # Already expired
        )

        # Build ClaimInfo from the claim
        info = ClaimInfo(
            event_type=claim.event_type,
            match=claim.match,
            owner_type=claim.owner_type,
            owner_class=claim.owner_class,
            owner_run_id=claim.owner_run_id,
            priority=claim.priority,
            expires_at=claim.expires_at,
            max_events=claim.max_events,
            events_handled=claim.events_handled,
            claimed_at=claim.claimed_at,
        )
        self.assertTrue(info.is_expired)

        # Test event-count expiry
        info2 = ClaimInfo(
            event_type="test",
            match={},
            owner_type="agent",
            owner_class="Test",
            owner_run_id=1,
            priority=0,
            expires_at=None,
            max_events=5,
            events_handled=5,  # Hit the limit
            claimed_at=timezone.now(),
        )
        self.assertTrue(info2.is_expired)

        # Test not expired
        info3 = ClaimInfo(
            event_type="test",
            match={},
            owner_type="agent",
            owner_class="Test",
            owner_run_id=1,
            priority=0,
            expires_at=timezone.now() + timedelta(hours=1),
            max_events=10,
            events_handled=5,
            claimed_at=timezone.now(),
        )
        self.assertFalse(info3.is_expired)

    def test_claim_with_no_time_expiry(self):
        """Test claim with expires_in=None has no time-based expiry"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent:
            pass

        with claims._context("agent", MockAgent, 123):
            claims.hold(
                "order_placed",
                match={"status": "confirmed"},
                expires_in=None
            )

        claim = EventClaim.objects.get(event_type="order_placed")
        self.assertIsNone(claim.expires_at)
        self.assertFalse(claim.is_expired)

    def test_complex_orm_filter_match(self):
        """Test claim with complex Django ORM filter syntax"""
        from django_ai.automation.events import claims

        class MockAgent:
            pass

        # Create booking with specific status
        booking = TestBooking.objects.create(
            guest_name="VIP Guest",
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="confirmed",
        )

        with claims._context("agent", MockAgent, 123):
            # Use __startswith filter
            claims.hold("booking_confirmed", match={"guest_name__startswith": "VIP"})

        matching_claim = claims.find_matching_claim("booking_confirmed", booking)
        self.assertIsNotNone(matching_claim)

        # Should not match our original booking
        non_matching = claims.find_matching_claim("booking_confirmed", self.booking)
        self.assertIsNone(non_matching)

    def test_multiple_claims_different_matches(self):
        """Test multiple claims for same event type with different match patterns"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent1:
            pass

        class MockAgent2:
            pass

        # Create two bookings
        booking1 = TestBooking.objects.create(
            guest_name="Guest One",
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="confirmed",
        )
        booking2 = TestBooking.objects.create(
            guest_name="Guest Two",
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="confirmed",
        )

        # Agent 1 claims events for Guest One
        with claims._context("agent", MockAgent1, 100):
            claims.hold("booking_confirmed", match={"guest_name": "Guest One"})

        # Agent 2 claims events for Guest Two
        with claims._context("agent", MockAgent2, 200):
            claims.hold("booking_confirmed", match={"guest_name": "Guest Two"})

        self.assertEqual(EventClaim.objects.count(), 2)

        # Each booking matches the correct claim
        claim1 = claims.find_matching_claim("booking_confirmed", booking1)
        self.assertEqual(claim1.owner_class, "MockAgent1")

        claim2 = claims.find_matching_claim("booking_confirmed", booking2)
        self.assertEqual(claim2.owner_class, "MockAgent2")

    def test_workflow_owner_type(self):
        """Test claims work with workflow owner type"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockWorkflow:
            pass

        with claims._context("workflow", MockWorkflow, 456):
            claims.hold("booking_confirmed", match={"guest_name": "Claim Test Guest"})

        claim = EventClaim.objects.get(event_type="booking_confirmed")
        self.assertEqual(claim.owner_type, "workflow")
        self.assertEqual(claim.owner_class, "MockWorkflow")
        self.assertEqual(claim.owner_run_id, 456)

    def test_claim_with_both_expiry_types(self):
        """Test claim with both time and event count expiry"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent:
            pass

        with claims._context("agent", MockAgent, 123):
            claims.hold(
                "order_placed",
                match={"status": "confirmed"},
                expires_in=timedelta(hours=1),
                expires_after_events=10
            )

        claim = EventClaim.objects.get(event_type="order_placed")
        self.assertIsNotNone(claim.expires_at)
        self.assertEqual(claim.max_events, 10)
        self.assertFalse(claim.is_expired)

    def test_increment_events_atomic(self):
        """Test that increment_events uses atomic F() expression"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent:
            pass

        with claims._context("agent", MockAgent, 123):
            claims.hold(
                "order_placed",
                match={"status": "confirmed"},
                expires_after_events=5
            )

        claim = EventClaim.objects.get(event_type="order_placed")
        self.assertEqual(claim.events_handled, 0)

        # Increment multiple times
        claim.increment_events()
        self.assertEqual(claim.events_handled, 1)

        claim.increment_events()
        claim.increment_events()
        self.assertEqual(claim.events_handled, 3)

    def test_find_matching_claim_skips_event_count_expired(self):
        """Test find_matching_claim skips claims expired by event count"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent:
            pass

        with claims._context("agent", MockAgent, 123):
            claims.hold(
                "booking_confirmed",
                match={"guest_name": "Claim Test Guest"},
                expires_in=None,
                expires_after_events=1
            )

        # First match should work
        claim = claims.find_matching_claim("booking_confirmed", self.booking)
        self.assertIsNotNone(claim)

        # Increment to expire it
        claim.increment_events()

        # Now should not find it
        claim2 = claims.find_matching_claim("booking_confirmed", self.booking)
        self.assertIsNone(claim2)

    def test_priority_ordering_in_find_matching_claim(self):
        """Test that find_matching_claim returns highest priority claim"""
        from django_ai.automation.events import claims
        from django_ai.automation.events.models import EventClaim

        class MockAgent1:
            pass

        class MockAgent2:
            pass

        # Create booking
        booking = TestBooking.objects.create(
            guest_name="Priority Test",
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="confirmed",
        )

        # Agent 1 claims with status=confirmed (lower priority)
        with claims._context("agent", MockAgent1, 100):
            claims.hold("booking_confirmed", match={"status": "confirmed"}, priority=5)

        # Agent 2 claims with guest_name (higher priority) - different match
        with claims._context("agent", MockAgent2, 200):
            claims.hold("booking_confirmed", match={"guest_name": "Priority Test"}, priority=10)

        # Both claims match the booking, but higher priority should be returned
        matching_claim = claims.find_matching_claim("booking_confirmed", booking)
        self.assertEqual(matching_claim.owner_class, "MockAgent2")
        self.assertEqual(matching_claim.priority, 10)


class EventWatchFieldsTest(TransactionTestCase):
    """Test the watch_fields functionality for UPDATE triggers"""

    def setUp(self):
        from django_ai.automation.events.callbacks import callback_registry
        callback_registry.clear()

    def tearDown(self):
        from django_ai.automation.events.callbacks import callback_registry
        callback_registry.clear()

    def test_event_definition_watch_fields_parameter(self):
        """Test EventDefinition accepts watch_fields parameter"""
        event_def = EventDefinition(
            "test_event",
            trigger=EventTrigger.UPDATE,
            watch_fields=["status", "priority"],
        )
        self.assertEqual(event_def.watch_fields, ["status", "priority"])

    def test_event_definition_watch_fields_default_none(self):
        """Test EventDefinition defaults watch_fields to None"""
        event_def = EventDefinition("test_event", trigger=EventTrigger.UPDATE)
        self.assertIsNone(event_def.watch_fields)

    def test_get_watched_values_hash_returns_none_without_watch_fields(self):
        """Test get_watched_values_hash returns None when no watch_fields defined"""
        event_def = EventDefinition("test_event", trigger=EventTrigger.UPDATE)
        instance = TestWatchFieldsModel(name="Test", status="pending", priority=1)
        self.assertIsNone(event_def.get_watched_values_hash(instance))

    def test_get_watched_values_hash_returns_hash_string(self):
        """Test get_watched_values_hash returns a SHA256 hash string"""
        event_def = EventDefinition(
            "test_event",
            trigger=EventTrigger.UPDATE,
            watch_fields=["status", "priority"],
        )
        instance = TestWatchFieldsModel(name="Test", status="active", priority=5)
        hash_value = event_def.get_watched_values_hash(instance)
        # Should be a 64-character hex string (SHA256)
        self.assertIsInstance(hash_value, str)
        self.assertEqual(len(hash_value), 64)
        # Should be deterministic
        self.assertEqual(hash_value, event_def.get_watched_values_hash(instance))

    def test_watched_values_hash_stored_on_event_creation(self):
        """Test that watched_values_hash is stored on Event when created"""
        instance = TestWatchFieldsModel.objects.create(
            name="Test",
            status="pending",
            priority=1,
        )

        # Get the status_changed event (has watch_fields=["status"])
        event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(TestWatchFieldsModel),
            entity_id=str(instance.id),
            event_name="status_changed",
        )

        # Should have a hash stored (64-char SHA256 hex)
        self.assertIsNotNone(event.watched_values_hash)
        self.assertEqual(len(event.watched_values_hash), 64)

    def test_update_without_watch_fields_fires_every_time(self):
        """Test UPDATE trigger without watch_fields fires on every save"""
        from django_ai.automation.events.callbacks import on_event

        callback_log = []

        @on_event(event_name="any_update")
        def on_any_update(event):
            callback_log.append(f"any_update:{event.entity_id}")

        # Create instance
        instance = TestWatchFieldsModel.objects.create(name="Test", status="pending")
        self.assertEqual(len(callback_log), 0)  # UPDATE doesn't fire on CREATE

        # First update - should fire
        instance.notes = "updated notes"
        instance.save()
        self.assertEqual(len(callback_log), 1)

        # Second update (same field) - should fire again
        instance.notes = "more notes"
        instance.save()
        self.assertEqual(len(callback_log), 2)

        # Third update (different field) - should fire again
        instance.name = "New Name"
        instance.save()
        self.assertEqual(len(callback_log), 3)

    def test_update_with_watch_fields_only_fires_on_change(self):
        """Test UPDATE trigger with watch_fields only fires when watched field changes"""
        from django_ai.automation.events.callbacks import on_event

        callback_log = []

        @on_event(event_name="status_changed")
        def on_status_changed(event):
            callback_log.append(f"status_changed:{event.entity_id}")

        # Create instance
        instance = TestWatchFieldsModel.objects.create(name="Test", status="pending")
        self.assertEqual(len(callback_log), 0)  # UPDATE doesn't fire on CREATE

        # Update non-watched field - should NOT fire
        instance.notes = "some notes"
        instance.save()
        self.assertEqual(len(callback_log), 0, "Should not fire when non-watched field changes")

        # Update watched field - should fire
        instance.status = "active"
        instance.save()
        self.assertEqual(len(callback_log), 1, "Should fire when watched field changes")

        # Update same watched field again - should fire
        instance.status = "completed"
        instance.save()
        self.assertEqual(len(callback_log), 2, "Should fire when watched field changes again")

        # Update without changing watched field value - should NOT fire
        instance.notes = "different notes"
        instance.save()
        self.assertEqual(len(callback_log), 2, "Should not fire when watched field unchanged")

    def test_update_with_multiple_watch_fields(self):
        """Test UPDATE trigger fires when any of the watched fields change"""
        from django_ai.automation.events.callbacks import on_event

        callback_log = []

        @on_event(event_name="important_fields_changed")
        def on_important_change(event):
            callback_log.append(f"important:{event.entity_id}")

        # Create instance
        instance = TestWatchFieldsModel.objects.create(
            name="Test",
            status="pending",
            priority=0,
        )
        self.assertEqual(len(callback_log), 0)

        # Update non-watched field - should NOT fire
        instance.notes = "notes"
        instance.save()
        self.assertEqual(len(callback_log), 0)

        # Update first watched field (status) - should fire
        instance.status = "active"
        instance.save()
        self.assertEqual(len(callback_log), 1)

        # Update second watched field (priority) - should fire
        instance.priority = 5
        instance.save()
        self.assertEqual(len(callback_log), 2)

        # Update both watched fields - should fire once
        instance.status = "completed"
        instance.priority = 10
        instance.save()
        self.assertEqual(len(callback_log), 3)

    def test_watched_values_hash_snapshot_updated_after_trigger(self):
        """Test that watched_values_hash snapshot is updated after event triggers"""
        instance = TestWatchFieldsModel.objects.create(name="Test", status="pending")

        # Get the event
        event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(TestWatchFieldsModel),
            entity_id=str(instance.id),
            event_name="status_changed",
        )
        initial_hash = event.watched_values_hash
        self.assertIsNotNone(initial_hash)

        # Update status
        instance.status = "active"
        instance.save()

        # Refresh and check snapshot was updated
        event.refresh_from_db()
        self.assertNotEqual(event.watched_values_hash, initial_hash)
        self.assertEqual(len(event.watched_values_hash), 64)

    def test_backward_compatibility_without_watch_fields(self):
        """Test that events without watch_fields still work (backward compatible)"""
        from django_ai.automation.events.callbacks import on_event

        callback_log = []

        @on_event(event_name="entity_modified")
        def on_modified(event):
            callback_log.append(f"modified:{event.entity_id}")

        # Use TestTriggerModel which has UPDATE trigger without watch_fields
        instance = TestTriggerModel.objects.create(name="Test", status="active")
        self.assertEqual(len(callback_log), 0)

        # Update - should fire (backward compatible behavior)
        instance.name = "Updated"
        instance.save()
        self.assertEqual(len(callback_log), 1)

        # Update again - should fire again
        instance.name = "Updated Again"
        instance.save()
        self.assertEqual(len(callback_log), 2)

    def test_watch_fields_hash_changes_with_foreign_key(self):
        """Test watch_fields hash changes when FK field changes"""
        event_def = EventDefinition(
            "test_event",
            trigger=EventTrigger.UPDATE,
            watch_fields=["guest"],
        )

        # Create a booking with guest
        guest1 = Guest.objects.create(email="test@example.com", name="Test Guest")
        guest2 = Guest.objects.create(email="other@example.com", name="Other Guest")
        booking = Booking.objects.create(
            guest=guest1,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
        )

        hash1 = event_def.get_watched_values_hash(booking)
        self.assertEqual(len(hash1), 64)

        # Change guest and verify hash changes
        booking.guest = guest2
        hash2 = event_def.get_watched_values_hash(booking)
        self.assertNotEqual(hash1, hash2)

    def test_watch_fields_hash_changes_with_datetime(self):
        """Test watch_fields hash changes when datetime field changes"""
        event_def = EventDefinition(
            "test_event",
            trigger=EventTrigger.UPDATE,
            watch_fields=["checkin_date"],
        )

        test_date = timezone.now()
        booking = TestBooking(
            guest_name="Test",
            checkin_date=test_date,
            checkout_date=test_date + timedelta(days=1),
        )

        hash1 = event_def.get_watched_values_hash(booking)
        self.assertEqual(len(hash1), 64)

        # Change datetime and verify hash changes
        booking.checkin_date = test_date + timedelta(hours=1)
        hash2 = event_def.get_watched_values_hash(booking)
        self.assertNotEqual(hash1, hash2)
