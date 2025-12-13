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

## Display Metadata - Building Rich UIs

Workflows provide comprehensive display metadata for building rich user interfaces. Add display information at three key points: completion, waiting, and individual steps.

### Completion Display

Add display messages when workflows complete:

```python
@step()
def finalize_order(self):
    ctx = get_context()
    
    return complete(
        display_title="Order Complete! 🎉",
        display_subtitle=f"Order #{ctx.order_id} has been successfully processed"
    )
```

**Dynamic messages using context:**
```python
@step()
def complete_processing(self):
    ctx = get_context()
    
    return complete(
        display_title=f"Welcome, {ctx.user_name}!",
        display_subtitle=f"Successfully processed {ctx.items_count} items in {ctx.duration}s"
    )
```

**Access completion display:**
```python
run = WorkflowRun.objects.get(id=workflow_id)
if run.status == WorkflowStatus.COMPLETED:
    print(run.completion_display['display_title'])     # "Order Complete! 🎉"
    print(run.completion_display['display_subtitle'])  # "Order #123 has been..."
```

### Waiting Display

Add display messages for waiting/suspended steps:

```python
@wait_for_event(
    "oauth_completed",
    timeout=timedelta(minutes=10),
    display_title="Authorization Required",
    display_subtitle="Please complete OAuth in the popup window",
    display_waiting_for="OAuth authorization"
)
@step()
def wait_for_oauth(self):
    return complete()
```

**Access waiting display:**
```python
run = WorkflowRun.objects.get(id=workflow_id)
if run.status == WorkflowStatus.SUSPENDED:
    print(run.waiting_display['display_title'])        # "Authorization Required"
    print(run.waiting_display['display_subtitle'])     # "Please complete OAuth..."
    print(run.waiting_display['display_waiting_for']) # "OAuth authorization"
```

**Use cases for waiting display:**
- External OAuth flows
- Payment processing
- Document verification
- Manual approval steps
- Third-party API callbacks

### Step Display Metadata

Add rich display information to action steps for form UIs:

```python
from django_ai.automation.workflows.metadata import StepDisplayMetadata, FieldGroup, FieldDisplayConfig
from django_ai.automation.workflows.statezero_action import statezero_action

class PropertySerializer(serializers.Serializer):
    workflow_run_id = serializers.IntegerField()
    property_name = serializers.CharField()
    property_type = serializers.CharField()
    bedrooms = serializers.IntegerField()
    email = serializers.EmailField()
    amenities = serializers.ListField()

@statezero_action(name="collect_property_info", serializer=PropertySerializer)
@step(
    start=True,
    display=StepDisplayMetadata(
        display_title="Set Up Your Property",
        display_description="Enter property details to get started with your listing",
        
        # Group related fields together
        field_groups=[
            FieldGroup(
                display_title="Property Information",
                display_description="Basic details about your listing",
                field_names=["property_name", "property_type", "bedrooms"]
            ),
            FieldGroup(
                display_title="Contact Details",
                display_description="How guests can reach you",
                field_names=["email"]
            )
        ],
        
        # Customize individual field display
        field_display_configs=[
            FieldDisplayConfig(
                field_name="amenities",
                display_component="AmenitiesMultiSelect",
                filter_queryset={"is_active": True, "category": "essential"},
                display_help_text="Select all amenities that apply"
            )
        ]
    )
)
def collect_property_info(self, property_name: str, property_type: str, 
                         bedrooms: int, email: str, amenities: list):
    ctx = get_context()
    ctx.property_name = property_name
    return goto(self.next_step)
```

**StepDisplayMetadata components:**

1. **Basic information:**
   - `display_title`: Main heading for the step
   - `display_description`: Explanatory text about what the user should do

2. **Field Groups (`FieldGroup`):**
   - `display_title`: Group heading
   - `display_description`: Group description
   - `field_names`: List of field names in this group
   - Groups fields logically for better UX (e.g., "Contact Info", "Address Details")

3. **Field Display Configs (`FieldDisplayConfig`):**
   - `field_name`: Which field this config applies to
   - `display_component`: Custom UI component name (e.g., "AddressAutocomplete", "DatePicker")
   - `filter_queryset`: Filter options for select/multi-select fields (dict passed to backend)
   - `display_help_text`: Additional help text for the field

**Access step display:**
```python
run = WorkflowRun.objects.get(id=workflow_id)
display = run.current_step_display

if display['step_display']:
    print(display['step_display']['display_title'])
    print(display['step_display']['display_description'])
    
    # Field groups
    for group in display['step_display']['field_groups']:
        print(f"Group: {group['display_title']}")
        print(f"Fields: {group['field_names']}")
    
    # Field configs
    for config in display['step_display']['field_display_configs']:
        print(f"Field: {config['field_name']}")
        print(f"Component: {config['display_component']}")
```

### The current_step_display Property

The `current_step_display` property provides all display metadata for the current workflow state:

```python
run = WorkflowRun.objects.get(id=workflow_id)
display = run.current_step_display

# Returns a dict with:
{
    "status": "running",              # WorkflowStatus
    "current_step": "collect_info",   # Step name
    "step_type": "action",            # "action", "automated", or "waiting"
    "progress": 0.5,                  # 0.0 to 1.0
    
    # One of these will be populated based on state:
    "completion_display": {...},      # If status == COMPLETED
    "waiting_display": {...},         # If status == SUSPENDED
    "step_display": {...},            # If current step has display metadata
}
```

**Example usage in an API:**
```python
from rest_framework.decorators import api_view

@api_view(['GET'])
def workflow_status(request, workflow_id):
    run = WorkflowRun.objects.get(id=workflow_id)
    return Response(run.current_step_display)
```

**Frontend can then render based on status:**
```typescript
// Pseudo frontend code
if (display.status === 'completed') {
    showSuccessScreen(display.completion_display);
} else if (display.status === 'suspended') {
    showWaitingScreen(display.waiting_display);
} else if (display.step_type === 'action') {
    showFormScreen(display.step_display);
} else {
    showProcessingSpinner();
}
```

### Display Metadata Best Practices

**Completion messages:**
- Use clear, positive language
- Include dynamic details from context (order numbers, user names)
- Keep titles short and punchy
- Use subtitles for more detail

**Waiting messages:**
- Explain what the workflow is waiting for
- Set expectations (e.g., "This usually takes 2-3 minutes")
- Provide context about what happens next
- Use `display_waiting_for` for technical tracking

**Step display:**
- Group related fields together
- Use custom components for complex inputs (date pickers, multi-selects)
- Provide helpful descriptions for each group
- Use `filter_queryset` to reduce options in dropdowns
- Add `display_help_text` for fields that need clarification

**Example: Complete workflow with rich display:**
```python
@workflow("property_onboarding")
class PropertyOnboardingWorkflow:
    class Context(BaseModel):
        property_id: int = 0
        property_name: str = ""
        owner_name: str = ""

    @statezero_action(name="property_details", serializer=PropertyDetailsSerializer)
    @step(
        start=True,
        display=StepDisplayMetadata(
            display_title="Property Details",
            display_description="Tell us about your property",
            field_groups=[
                FieldGroup(
                    display_title="Basic Information",
                    field_names=["property_name", "property_type"]
                )
            ]
        )
    )
    def collect_details(self, property_name: str, property_type: str):
        ctx = get_context()
        ctx.property_name = property_name
        return goto(self.verify_ownership)

    @wait_for_event(
        "ownership_verified",
        timeout=timedelta(minutes=30),
        display_title="Verifying Ownership",
        display_subtitle="Please complete verification in your email",
        display_waiting_for="Email verification"
    )
    @step()
    def verify_ownership(self):
        return goto(self.finalize)

    @step()
    def finalize(self):
        ctx = get_context()
        return complete(
            display_title="Welcome to the Platform! 🎉",
            display_subtitle=f"{ctx.property_name} is now live and ready for bookings"
        )
```

---

## Progress Tracking

Report workflow completion percentage using the optional `progress` parameter:

```python
@step()
def step_one(self):
    return goto(self.step_two, progress=0.25)

@step()
def step_two(self):
    return goto(self.step_three, progress=0.50)

@step()
def step_three(self):
    return goto(self.step_four, progress=0.75)

@step()
def step_four(self):
    return complete()  # Automatically sets progress to 1.0
```

**Access progress:**
```python
run = WorkflowRun.objects.get(id=workflow_id)
percentage = run.progress * 100  # Convert 0.0-1.0 to percentage
print(f"Workflow is {percentage:.0f}% complete")
```

**Combine with display metadata:**
```python
display = run.current_step_display
return {
    "progress": run.progress * 100,
    "status": display['status'],
    "message": display.get('waiting_display', {}).get('display_title', 'Processing...')
}
```

---

## Data Merging

### Context Merging in complete()

The `complete()` function accepts keyword arguments that merge into your workflow context:

```python
@step()
def finalize_order(self):
    ctx = get_context()
    
    # These kwargs merge into context fields
    return complete(
        final_total=ctx.calculated_total,
        completion_timestamp=timezone.now().isoformat(),
        order_status="completed",
        display_title="Order Complete!",
        display_subtitle=f"Total: ${ctx.calculated_total}"
    )
```

**Your Context must have these fields:**
```python
class OrderContext(BaseModel):
    final_total: Decimal = Decimal("0")
    completion_timestamp: str = ""
    order_status: str = "pending"
```

Only fields that exist in your Context class will be merged. Display fields (`display_title`, `display_subtitle`) are handled separately.

### Context Merging in Signals

When sending signals, payload data merges into the workflow context:

```python
@step()
def wait_for_review(self):
    return wait.for_signal("review_completed")

@step()
def process_review(self):
    ctx = get_context()
    # These fields came from the signal payload
    print(f"Reviewed by: {ctx.reviewer_name}")
    print(f"Notes: {ctx.review_notes}")
    print(f"Approved: {ctx.approved}")
    return goto(self.next_step)
```

**Send the signal with data:**
```python
engine.signal("review_completed", {
    "reviewer_name": "Jane Smith",
    "review_notes": "Looks good!",
    "approved": True
})
```

**Context definition:**
```python
class ReviewContext(BaseModel):
    reviewer_name: str = ""
    review_notes: str = ""
    approved: bool = False
```

---

## Step Execution History

Workflows automatically track completed and failed steps:

```python
from django_ai.automation.workflows.models import StepExecution

run = WorkflowRun.objects.get(id=workflow_id)
executions = run.step_executions.all()

for execution in executions:
    status_emoji = "✅" if execution.status == "completed" else "❌"
    print(f"{status_emoji} {execution.step_name} - {execution.created_at}")
    if execution.error:
        print(f"   Error: {execution.error}")
```

**What gets recorded:**
- ✅ Steps that complete successfully
- ❌ Steps that fail (with error message)
- 🚫 NOT sleep() transitions
- 🚫 NOT steps that suspend (waiting/action steps)

---

## Error Handling & Retries

### Workflow-Level Retry Policy

```python
from django_ai.automation.workflows.core import Retry

@workflow("api_integration", default_retry=Retry(
    max_attempts=5,
    base_delay=timedelta(seconds=2),
    max_delay=timedelta(minutes=5),
    backoff_factor=2.0
))
class ApiIntegrationWorkflow:
    pass
```

### Step-Level Retry Override

```python
@step(retry=Retry(max_attempts=10, base_delay=timedelta(seconds=1)))
def call_external_api(self):
    ctx = get_context()
    response = make_api_call(ctx.endpoint)
    if response.status_code != 200:
        raise Exception(f"API call failed: {response.status_code}")
    return goto(self.next_step)
```

---

## Compensation (on_fail Handler)

When a workflow fails, you may need to clean up or reverse actions that were already completed. The `@on_fail` decorator marks a method that runs when the workflow fails, giving you access to the workflow's step history to decide what compensation is needed.

### Basic Usage

```python
from django_ai.automation.workflows.core import workflow, step, on_fail, get_context, fail, goto

@workflow("order_processing")
class OrderProcessingWorkflow:
    class Context(BaseModel):
        order_id: int
        charge_id: Optional[str] = None
        reservation_id: Optional[str] = None

    @step(start=True)
    def charge_payment(self):
        ctx = get_context()
        ctx.charge_id = payment_service.charge(ctx.order_id)
        return goto(self.reserve_inventory)

    @step()
    def reserve_inventory(self):
        ctx = get_context()
        ctx.reservation_id = inventory_service.reserve(ctx.order_id)
        return goto(self.ship_order)

    @step()
    def ship_order(self):
        # This might fail!
        shipping_service.ship(ctx.order_id)
        return complete()

    @on_fail
    def cleanup(self, run: WorkflowRun):
        """Called when workflow fails - inspect history and compensate."""
        # See which steps completed
        completed_steps = set(
            run.step_executions.filter(status='completed')
            .values_list('step_name', flat=True)
        )

        # Access context for IDs we need to reverse
        ctx = get_context()

        # Reverse completed actions
        if 'charge_payment' in completed_steps and ctx.charge_id:
            payment_service.refund(ctx.charge_id)

        if 'reserve_inventory' in completed_steps and ctx.reservation_id:
            inventory_service.release(ctx.reservation_id)
```

### When on_fail is Called

The `@on_fail` handler runs in two scenarios:

1. **Explicit failure**: When a step returns `fail("reason")`
2. **Exception with max retries exhausted**: When a step throws an exception and all retry attempts have been used

```python
@step()
def risky_step(self):
    if something_wrong:
        return fail("Validation failed")  # Triggers on_fail

    raise ValueError("Boom!")  # Also triggers on_fail (after retries exhausted)
```

### Accessing Step History

The `WorkflowRun` passed to `@on_fail` includes the full step execution history:

```python
@on_fail
def cleanup(self, run: WorkflowRun):
    # Get all completed steps
    completed = run.step_executions.filter(status='completed')

    for execution in completed:
        print(f"Step: {execution.step_name}")
        print(f"Completed at: {execution.created_at}")

    # Get the failed step
    failed = run.step_executions.filter(status='failed').first()
    if failed:
        print(f"Failed step: {failed.step_name}")
        print(f"Error: {failed.error}")
```

### Modifying Context in on_fail

You can modify the workflow context in the `@on_fail` handler - useful for recording cleanup actions:

```python
@on_fail
def cleanup(self, run: WorkflowRun):
    ctx = get_context()

    # Track what we cleaned up
    ctx.cleanup_performed = True
    ctx.refunded = False

    if ctx.charge_id:
        payment_service.refund(ctx.charge_id)
        ctx.refunded = True
```

### Error Handling in on_fail

Errors in the `@on_fail` handler are logged but don't change the workflow's failure status - the original error is preserved:

```python
@on_fail
def cleanup(self, run: WorkflowRun):
    # If this raises, it's logged but workflow still shows original error
    external_service.notify_failure(run.id)
```

### Rules and Limitations

- **One handler per workflow**: Only one method can be decorated with `@on_fail`
- **Not a step**: The `@on_fail` method is not a workflow step - don't use `@step()` on it
- **Can't change outcome**: The workflow will still be marked as failed after `@on_fail` runs
- **Runs synchronously**: The handler runs before the workflow is marked as failed in the database

```python
# This will raise an error at workflow registration time
@workflow("bad_example")
class BadWorkflow:
    @on_fail
    def cleanup1(self, run): pass

    @on_fail
    def cleanup2(self, run): pass  # Error: multiple @on_fail handlers
```

### Best Practices

**1. Check what actually completed:**
```python
@on_fail
def cleanup(self, run: WorkflowRun):
    completed = set(
        run.step_executions.filter(status='completed')
        .values_list('step_name', flat=True)
    )

    # Only reverse what actually happened
    if 'charge_payment' in completed:
        self._refund_payment()
```

**2. Make compensation idempotent:**
```python
@on_fail
def cleanup(self, run: WorkflowRun):
    ctx = get_context()

    # Safe to call multiple times
    if ctx.charge_id and not ctx.already_refunded:
        payment_service.refund(ctx.charge_id)
        ctx.already_refunded = True
```

**3. Log compensation actions:**
```python
@on_fail
def cleanup(self, run: WorkflowRun):
    ctx = get_context()
    logger.info(f"Running compensation for workflow {run.id}")

    if ctx.charge_id:
        logger.info(f"Refunding charge {ctx.charge_id}")
        payment_service.refund(ctx.charge_id)
```

**4. Handle external service failures gracefully:**
```python
@on_fail
def cleanup(self, run: WorkflowRun):
    ctx = get_context()

    try:
        if ctx.charge_id:
            payment_service.refund(ctx.charge_id)
    except PaymentServiceError as e:
        # Log but don't re-raise - manual intervention needed
        logger.error(f"Failed to refund {ctx.charge_id}: {e}")
        ctx.manual_refund_needed = True
```

---

## Event-Triggered Workflows

### Basic Event Workflow

```python
@event_workflow("order_placed")
class OrderProcessingWorkflow:
    class Context(BaseModel):
        order_id: int
        
    @classmethod  
    def create_context(cls, event=None):
        order = event.entity
        return cls.Context(order_id=order.id)
    
    @step(start=True)
    def process_new_order(self):
        return complete()
```

### Event Workflows with Timing Offsets

```python
@event_workflow("appointment_due", offset=timedelta(minutes=-60))
class PreAppointmentReminder:
    @step(start=True)
    def send_reminder(self):
        return complete(
            display_title="Reminder Sent",
            display_subtitle="Customer notified 1 hour before appointment"
        )
```

---

## StateZero Integration

Expose workflow steps as API endpoints:

```python
from django_ai.automation.workflows.statezero_action import statezero_action

class ReviewSerializer(serializers.Serializer):
    workflow_run_id = serializers.IntegerField()
    reviewer_notes = serializers.CharField(max_length=500)
    priority = serializers.ChoiceField(choices=["low", "medium", "high"])

@workflow("expense_approval")
class ExpenseApprovalWorkflow:
    class Context(BaseModel):
        reviewer_notes: str = ""
        priority: str = "medium"

    @step(start=True)
    def start_review(self):
        return goto(self.await_review)

    @statezero_action(name="submit_review", serializer=ReviewSerializer)
    @step(
        display=StepDisplayMetadata(
            display_title="Review Expense",
            display_description="Please review and approve or reject",
            field_groups=[
                FieldGroup(
                    display_title="Review Details",
                    field_names=["reviewer_notes", "priority"]
                )
            ]
        )
    )
    def await_review(self, reviewer_notes: str, priority: str):
        ctx = get_context()
        ctx.reviewer_notes = reviewer_notes
        ctx.priority = priority
        return complete(
            display_title="Review Complete",
            display_subtitle="Expense has been reviewed"
        )

# POST /statezero/actions/submit_review/
# {"workflow_run_id": 123, "reviewer_notes": "Approved", "priority": "high"}
```

---

## Subworkflows (Workflow Composition)

Workflows can launch other workflows as subworkflows, enabling reusable workflow components and complex multi-level orchestration.

### Basic Subworkflow Example

```python
from django_ai.automation.workflows.core import run_subflow, SubflowResult

# Reusable subworkflow
@workflow("send_notification")
class SendNotificationWorkflow:
    class Context(BaseModel):
        recipient_email: str
        message: str
        sent: bool = False

    @step(start=True)
    def send_email(self):
        ctx = get_context()
        # Email sending logic
        ctx.sent = True
        return complete()

# Parent workflow that uses the subworkflow
@workflow("user_registration")
class UserRegistrationWorkflow:
    class Context(BaseModel):
        user_email: str
        user_id: int = 0
        active_subworkflow_run_id: Optional[int] = None

    @step(start=True)
    def create_user(self):
        ctx = get_context()
        # User creation logic
        ctx.user_id = 123

        # Launch subworkflow
        return run_subflow(
            SendNotificationWorkflow,
            on_complete=self.after_notification,
            recipient_email=ctx.user_email,
            message="Welcome to our platform!"
        )

    @step()
    def after_notification(self, subflow_result: SubflowResult):
        ctx = get_context()

        # Access the subworkflow's result
        if subflow_result.status == WorkflowStatus.COMPLETED:
            notification_ctx = subflow_result.context
            print(f"Email sent: {notification_ctx.sent}")
            return complete()
        else:
            # Handle failure
            return fail(f"Notification failed: {subflow_result.error}")
```

### How Subworkflows Work

1. **Launch**: `run_subflow()` creates a new workflow run as a child
2. **Suspend**: Parent workflow suspends and waits for child to complete
3. **Execute**: Child workflow runs independently
4. **Signal**: When child completes/fails, it signals the parent
5. **Resume**: Parent resumes at the `on_complete` step with results

### SubflowResult Parameter

The `on_complete` step receives a `SubflowResult` object with:

```python
@step()
def handle_subflow(self, subflow_result: SubflowResult):
    # Check completion status
    if subflow_result.status == WorkflowStatus.COMPLETED:
        # Access child workflow's context (fully typed)
        child_data = subflow_result.context
        print(f"Child output: {child_data.some_field}")

    elif subflow_result.status == WorkflowStatus.FAILED:
        # Handle failure
        print(f"Child failed: {subflow_result.error}")

    # Access child run ID if needed
    child_run_id = subflow_result.run_id

    return complete()
```

**SubflowResult fields:**
- `status`: `WorkflowStatus.COMPLETED` or `WorkflowStatus.FAILED`
- `context`: The child workflow's final context (typed as child's Context model)
- `run_id`: The child workflow run ID
- `error`: Error message if the child failed (None otherwise)

### Nested Subworkflows

Subworkflows can launch their own subworkflows, creating multi-level hierarchies:

```python
@workflow("payment_processing")
class PaymentWorkflow:
    class Context(BaseModel):
        amount: Decimal
        processed: bool = False

    @step(start=True)
    def process(self):
        ctx = get_context()
        ctx.processed = True
        return complete()

@workflow("order_fulfillment")
class FulfillmentWorkflow:
    class Context(BaseModel):
        order_id: int
        payment_complete: bool = False
        active_subworkflow_run_id: Optional[int] = None

    @step(start=True)
    def start_payment(self):
        ctx = get_context()
        return run_subflow(
            PaymentWorkflow,
            on_complete=self.after_payment,
            amount=Decimal("99.99")
        )

    @step()
    def after_payment(self, subflow_result: SubflowResult):
        ctx = get_context()
        ctx.payment_complete = subflow_result.context.processed
        return complete()

@workflow("order_management")
class OrderWorkflow:
    class Context(BaseModel):
        customer_id: int
        order_complete: bool = False
        active_subworkflow_run_id: Optional[int] = None

    @step(start=True)
    def start_fulfillment(self):
        return run_subflow(
            FulfillmentWorkflow,
            on_complete=self.finalize,
            order_id=456
        )

    @step()
    def finalize(self, subflow_result: SubflowResult):
        ctx = get_context()
        ctx.order_complete = subflow_result.context.payment_complete
        return complete()
```

### Tracking Active Subworkflows

The parent context can optionally track the active subworkflow:

```python
class Context(BaseModel):
    active_subworkflow_run_id: Optional[int] = None
```

This field is automatically set when a subworkflow is launched and can be used to query the child workflow from the database if needed (though `SubflowResult` provides the most common data).

### Subworkflow Best Practices

**When to use subworkflows:**
- ✅ Reusable workflow components (notifications, payments, verifications)
- ✅ Breaking complex workflows into manageable pieces
- ✅ Workflows that need to be triggered independently or as part of larger flows
- ✅ Multi-tenant scenarios where different tenants use different sub-processes

**Design tips:**
- Keep subworkflows focused and single-purpose
- Use typed Context models so parent workflows get type safety
- Handle both success and failure cases in `on_complete` steps
- Consider making subworkflows event-triggered so they can run standalone too

**Example: Reusable verification subworkflow:**
```python
@workflow("email_verification")
class EmailVerificationWorkflow:
    """Reusable email verification - can be used by multiple parent workflows"""

    class Context(BaseModel):
        email: str
        verification_code: str = ""
        verified: bool = False

    @step(start=True)
    def send_code(self):
        ctx = get_context()
        ctx.verification_code = generate_code()
        send_email(ctx.email, ctx.verification_code)
        return goto(self.wait_for_verification)

    @wait_for_event("email_verified", timeout=timedelta(hours=24))
    @step()
    def wait_for_verification(self):
        ctx = get_context()
        ctx.verified = True
        return complete()

# Used by user registration
@workflow("user_registration")
class UserRegistrationWorkflow:
    @step()
    def verify_email(self):
        ctx = get_context()
        return run_subflow(
            EmailVerificationWorkflow,
            on_complete=self.after_verification,
            email=ctx.email
        )

    @step()
    def after_verification(self, subflow_result: SubflowResult):
        if not subflow_result.context.verified:
            return fail("Email verification failed")
        return complete()

# Also used by password reset
@workflow("password_reset")
class PasswordResetWorkflow:
    @step()
    def verify_email(self):
        ctx = get_context()
        return run_subflow(
            EmailVerificationWorkflow,
            on_complete=self.after_verification,
            email=ctx.email
        )

    @step()
    def after_verification(self, subflow_result: SubflowResult):
        if subflow_result.context.verified:
            return goto(self.reset_password)
        return fail("Verification failed")
```

---

## Summary: Complete Display Metadata Example

```python
@workflow("comprehensive_workflow")
class ComprehensiveWorkflow:
    class Context(BaseModel):
        user_name: str = ""
        items_count: int = 0

    @statezero_action(name="collect_info", serializer=InfoSerializer)
    @step(
        start=True,
        display=StepDisplayMetadata(
            display_title="Welcome!",
            display_description="Let's get started",
            field_groups=[
                FieldGroup(
                    display_title="Your Information",
                    field_names=["user_name"]
                )
            ]
        )
    )
    def collect_info(self, user_name: str):
        ctx = get_context()
        ctx.user_name = user_name
        return goto(self.process, progress=0.33)

    @step()
    def process(self):
        ctx = get_context()
        ctx.items_count = 42
        return goto(self.wait_for_confirmation, progress=0.67)

    @wait_for_event(
        "confirmation_received",
        display_title="Waiting for Confirmation",
        display_subtitle="Please check your email",
        display_waiting_for="Email confirmation"
    )
    @step()
    def wait_for_confirmation(self):
        return goto(self.finish)

    @step()
    def finish(self):
        ctx = get_context()
        return complete(
            display_title=f"All Done, {ctx.user_name}!",
            display_subtitle=f"Successfully processed {ctx.items_count} items"
        )

# Access all display metadata:
run = WorkflowRun.objects.get(id=workflow_id)
display = run.current_step_display
# Returns complete display information for current workflow state
```