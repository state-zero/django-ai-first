# Workflows Developer Guide

> Build durable, multi-step processes with automatic persistence, retries, and complex control flows.

Workflows handle business processes that span multiple steps, require retries, or need to wait for external events. Unlike single-step automations, workflows maintain state across steps and can pause/resume execution.

---

## Quick Start

### 1. Basic Workflow Structure

```python
# workflows.py
from pydantic import BaseModel
from django_ai.automation.workflows.core import workflow, step, get_context, complete

@workflow("user_onboarding")
class UserOnboardingWorkflow:
    class Context(BaseModel):
        user_id: int
        email_sent: bool = False
        profile_completed: bool = False
        
    @classmethod
    def create_context(cls, user_id: int):
        return cls.Context(user_id=user_id)
    
    @step(start=True)
    def send_welcome_email(self):
        ctx = get_context()
        # Send email logic here
        ctx.email_sent = True
        return complete()

# Start manually
from django_ai.automation.workflows.core import engine
run = engine.start("user_onboarding", user_id=123)
```

### 2. Multi-Step Flow

```python
from django_ai.automation.workflows.core import goto

@workflow("order_fulfillment")
class OrderFulfillmentWorkflow:
    class Context(BaseModel):
        order_id: int
        payment_confirmed: bool = False
        items_reserved: bool = False
        shipped: bool = False
    
    @step(start=True)
    def confirm_payment(self):
        ctx = get_context()
        # Payment processing logic
        ctx.payment_confirmed = True
        return goto(self.reserve_inventory)
    
    @step()
    def reserve_inventory(self):
        ctx = get_context()
        # Inventory logic
        ctx.items_reserved = True
        return goto(self.ship_order)
    
    @step()
    def ship_order(self):
        ctx = get_context()
        # Shipping logic
        ctx.shipped = True
        return complete()
```

---

## Control Flow Commands

### Navigation Between Steps

```python
from django_ai.automation.workflows.core import goto

@step()
def process_order(self):
    ctx = get_context()
    
    if ctx.order_total > 1000:
        return goto(self.require_approval)
    else:
        return goto(self.fulfill_immediately)

@step()
def require_approval(self):
    # Approval logic
    return goto(self.fulfill_immediately)

@step()
def fulfill_immediately(self):
    # Fulfillment logic
    return complete()
```

### Completion and Failure

```python
from django_ai.automation.workflows.core import complete, fail

@step()
def final_step(self):
    ctx = get_context()
    
    if ctx.all_validations_passed:
        # Merge final data into context
        return complete(
            final_status="approved",
            completion_date=timezone.now().isoformat()
        )
    else:
        return fail("Validation failed: missing required documents")
```

---

## Time-Based Operations

### Sleep/Delays

```python
from datetime import timedelta
from django_ai.automation.workflows.core import sleep

@step()
def send_reminder(self):
    # Send initial reminder
    ctx = get_context()
    ctx.reminders_sent += 1
    
    if ctx.reminders_sent < 3:
        # Wait 24 hours before next reminder
        return sleep(timedelta(hours=24))
    else:
        return goto(self.escalate)
```

### Waiting for Signals

```python
from django_ai.automation.workflows.core import wait

@step()
def wait_for_approval(self):
    # Wait indefinitely for approval signal
    return wait.for_signal("manager_approval")

@step()
def wait_with_timeout(self):
    # Wait for signal with timeout fallback
    return wait.for_signal(
        "user_response",
        timeout=timedelta(days=7),
        on_timeout="send_final_notice"
    )

@step()
def send_final_notice(self):
    # Timeout handler
    return complete(status="expired")

# Send signals from elsewhere in your code
from django_ai.automation.workflows.core import engine
engine.signal("manager_approval", {"approved": True, "notes": "Looks good"})
```

### Waiting for Events

```python
from django_ai.automation.workflows.core import wait_for_event

@step()
def wait_for_payment(self):
    # Wait for payment_received event to occur
    return wait_for_event(
        "payment_received",
        timeout=timedelta(hours=48),
        on_timeout="cancel_order"
    )

@step()
def process_payment(self):
    ctx = get_context()
    # Payment was received, context automatically updated with event data
    # ctx.event_id, ctx.event_name, ctx.entity_id are available
    return goto(self.fulfill_order)
```

---

## Error Handling & Retries

### Workflow-Level Retry Policy

```python
from django_ai.automation.workflows.core import Retry

@workflow("api_integration", default_retry=Retry(
    max_attempts=5,
    base_delay=timedelta(seconds=2),
    max_delay=timedelta(minutes=5),
    backoff_factor=2.0  # Exponential backoff
))
class ApiIntegrationWorkflow:
    # If any step fails, retry up to 5 times with exponential backoff
    pass
```

### Step-Level Retry Override

```python
@step(retry=Retry(max_attempts=10, base_delay=timedelta(seconds=1)))
def call_external_api(self):
    ctx = get_context()
    
    # This step will retry up to 10 times if it raises an exception
    response = make_api_call(ctx.endpoint)
    
    if response.status_code != 200:
        raise Exception(f"API call failed: {response.status_code}")
    
    ctx.api_response = response.json()
    return goto(self.process_response)
```

### Graceful Error Handling

```python
@step()
def risky_operation(self):
    ctx = get_context()
    
    try:
        result = perform_operation()
        ctx.operation_result = result
        return goto(self.success_step)
    except SpecificException as e:
        ctx.error_details = str(e)
        return goto(self.error_recovery_step)
    except Exception as e:
        # Let other exceptions trigger retry mechanism
        raise
```

---

## Event-Triggered Workflows

### Basic Event Workflow

```python
from django_ai.automation.workflows.core import event_workflow

@event_workflow("order_placed")
class OrderProcessingWorkflow:
    class Context(BaseModel):
        order_id: int
        
    @classmethod  
    def create_context(cls, event=None):
        # event.entity gives you the model instance that triggered the event
        order = event.entity
        return cls.Context(order_id=order.id)
    
    @step(start=True)
    def process_new_order(self):
        ctx = get_context()
        # Process the order
        return complete()
```

### Event Workflows with Timing Offsets

```python
# Run 1 hour before the event time
@event_workflow("appointment_due", offset_minutes=-60)
class PreAppointmentReminder:
    @step(start=True)
    def send_reminder(self):
        # Runs 1 hour before appointment_due event time
        return complete()

# Run 30 minutes after the event time  
@event_workflow("meeting_ended", offset_minutes=30)
class PostMeetingFollowup:
    @step(start=True)
    def send_followup(self):
        # Runs 30 minutes after meeting_ended event time
        return complete()
```

---

## Workflow State & Context

### Context Design

```python
class OrderContext(BaseModel):
    # Required fields
    order_id: int
    customer_email: str
    
    # Optional fields with defaults
    payment_method: str = ""
    total_amount: Decimal = Decimal('0.00')
    shipping_address: dict = {}
    
    # Workflow state tracking
    steps_completed: list = []
    retry_count: int = 0
    last_error: str = ""
    
    # External system IDs
    payment_processor_id: str = ""
    shipping_tracking_id: str = ""
```

### Context Access and Updates

```python
@step()
def update_context_example(self):
    ctx = get_context()
    
    # Read context
    order_id = ctx.order_id
    current_steps = ctx.steps_completed
    
    # Update context
    ctx.payment_method = "credit_card"
    ctx.steps_completed.append("payment_processed")
    ctx.shipping_address = {
        "street": "123 Main St",
        "city": "Portland", 
        "state": "OR"
    }
    
    # Context changes are automatically persisted
    return goto(self.next_step)
```

### Context in Signal Payloads

```python
@step()
def wait_for_user_input(self):
    return wait.for_signal("user_submitted_form")

# When signal is sent, payload merges into context
engine.signal("user_submitted_form", {
    "form_data": {"name": "John", "phone": "555-1234"},
    "submission_time": timezone.now().isoformat()
})

@step()
def process_user_input(self):
    ctx = get_context()
    # ctx.form_data and ctx.submission_time are now available
    user_name = ctx.form_data["name"]
    return complete()
```

---

## Advanced Patterns

### State Machine Pattern

```python
@workflow("loan_application")
class LoanApplicationWorkflow:
    class Context(BaseModel):
        application_id: int
        state: str = "submitted"
        credit_score: int = 0
        income_verified: bool = False
        decision: str = ""
    
    @step(start=True)
    def submitted_state(self):
        ctx = get_context()
        ctx.state = "under_review"
        return goto(self.check_credit)
    
    @step()
    def check_credit(self):
        ctx = get_context()
        # Credit check logic
        if ctx.credit_score > 700:
            ctx.state = "approved"
            return goto(self.approved_state)
        elif ctx.credit_score > 600:
            ctx.state = "manual_review"
            return goto(self.manual_review_state)
        else:
            ctx.state = "denied"
            return goto(self.denied_state)
    
    @step()
    def approved_state(self):
        return complete()
    
    @step()
    def manual_review_state(self):
        return wait.for_signal("manual_review_complete")
    
    @step()
    def denied_state(self):
        return complete()
```

### Human-in-the-Loop Pattern

```python
@workflow("document_approval")
class DocumentApprovalWorkflow:
    class Context(BaseModel):
        document_id: int
        reviewer_assigned: str = ""
        reviewer_notes: str = ""
        approved: bool = False
    
    @step(start=True)
    def assign_reviewer(self):
        ctx = get_context()
        ctx.reviewer_assigned = get_next_available_reviewer()
        send_review_request_email(ctx.reviewer_assigned, ctx.document_id)
        
        return wait.for_signal(
            "review_completed",
            timeout=timedelta(days=3),
            on_timeout="escalate_review"
        )
    
    @step()
    def process_review(self):
        ctx = get_context()
        # reviewer_notes and approved come from signal payload
        if ctx.approved:
            return goto(self.approve_document)
        else:
            return goto(self.request_revision)
    
    @step()
    def escalate_review(self):
        # Timeout handler - escalate to manager
        return wait.for_signal("manager_review_completed")
    
    @step()
    def approve_document(self):
        return complete()
    
    @step()
    def request_revision(self):
        return complete()

# External review system sends signal
def submit_review(document_id, approved, notes):
    engine.signal("review_completed", {
        "approved": approved,
        "reviewer_notes": notes
    })
```

---

## Monitoring & Debugging

### Workflow Status Checking

```python
from django_ai.automation.workflows.models import WorkflowRun, WorkflowStatus

# Check workflow status
run = WorkflowRun.objects.get(id=workflow_run_id)
print(f"Status: {run.status}")
print(f"Current step: {run.current_step}")
print(f"Context data: {run.data}")

# Find failed workflows
failed_runs = WorkflowRun.objects.filter(status=WorkflowStatus.FAILED)
for run in failed_runs:
    print(f"Failed workflow {run.name}: {run.error}")

# Find waiting workflows
waiting_runs = WorkflowRun.objects.filter(status=WorkflowStatus.WAITING)
for run in waiting_runs:
    if run.waiting_signal:
        print(f"Waiting for signal: {run.waiting_signal}")
    if run.wake_at:
        print(f"Will wake at: {run.wake_at}")
```

### Manual Workflow Control

```python
# Cancel a running workflow
engine.cancel(workflow_run_id)

# Send signals to wake up waiting workflows
engine.signal("approval_received", {"approved": True})

# Process scheduled workflows manually (useful for testing)
engine.process_scheduled()
```

### Testing Workflows

```python
# tests.py
from django.test import TestCase
from django_ai.automation.workflows.core import engine
from django_ai.automation.queues.sync_executor import SynchronousExecutor

class WorkflowTestCase(TestCase):
    def setUp(self):
        # Use synchronous executor for tests
        engine.set_executor(SynchronousExecutor())
    
    def test_order_workflow(self):
        # Start workflow
        run = engine.start("order_fulfillment", order_id=123)
        
        # Check it's running
        self.assertEqual(run.status, "running")
        self.assertEqual(run.current_step, "confirm_payment")
        
        # Send signal to advance
        engine.signal("payment_confirmed")
        
        # Check final state
        run.refresh_from_db()
        self.assertEqual(run.status, "completed")
```

---

## StateZero Integration

The workflows system integrates with StateZero to expose workflow steps as API endpoints:

```python
from django_ai.automation.workflows.statezero_action import statezero_action
from rest_framework import serializers

class ReviewInputSerializer(serializers.Serializer):
    workflow_run_id = serializers.IntegerField()
    reviewer_notes = serializers.CharField(max_length=500)
    priority = serializers.ChoiceField(choices=["low", "medium", "high"])

@workflow("expense_approval")
class ExpenseApprovalWorkflow:
    class Context(BaseModel):
        expense_id: int = 123
        reviewer_notes: str = ""
        priority: str = "medium"
        status: str = "pending"

    @step(start=True)
    def start_review(self):
        return goto(self.await_review)

    @statezero_action(name="expense_submit_review", serializer=ReviewInputSerializer)
    @step()
    def await_review(self, reviewer_notes: str, priority: str):
        """Step callable as StateZero action"""
        ctx = get_context()
        ctx.reviewer_notes = reviewer_notes
        ctx.priority = priority
        ctx.status = "reviewed"
        return complete()

# This creates an API endpoint at /statezero/actions/expense_submit_review/
# POST with: {"workflow_run_id": 123, "reviewer_notes": "Approved", "priority": "high"}
```

---

## Best Practices

### Context Design
- Keep context fields flat and simple
- Use primitive types (str, int, bool) when possible
- Avoid storing large objects - store IDs and fetch when needed
- Include state tracking fields for complex workflows

### Error Handling
- Use specific retry policies for different types of operations
- Handle expected errors gracefully with `try/catch` and `goto`
- Let unexpected errors bubble up to trigger retry mechanism
- Include error details in context for debugging

### Performance
- Minimize database queries in step methods
- Use `select_related`/`prefetch_related` when fetching related objects
- Keep step methods focused and fast
- Use appropriate timeout values for waits

### Monitoring
- Log important state changes in step methods
- Use meaningful workflow and step names
- Include business context in error messages
- Monitor workflow queues and processing times

That's the workflows system! It provides powerful orchestration capabilities while keeping the developer experience simple and intuitive.