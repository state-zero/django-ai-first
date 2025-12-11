import inspect
import logging
from rest_framework import serializers
from rest_framework.exceptions import ValidationError as DRFValidationError
from statezero.core.actions import action
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError
from .core import (
    engine,
    get_context,
    _workflows,
    WorkflowContextManager,
    _current_context,
)
from .models import WorkflowRun, WorkflowStatus

logger = logging.getLogger(__name__)


def statezero_action(
    name=None, serializer=None, response_serializer=None, permissions=None, display=None
):
    """
    Decorator that makes a workflow step callable as a StateZero action.

    Automatically handles workflow_run_id and calls the step with remaining arguments.
    The workflow is expected to be in a SUSPENDED state before this action can be called.

    Usage:
    @statezero_action(
        name="expense_submit_review",
        serializer=ReviewInputSerializer,
        display=DisplayMetadata(...)
    )
    @step()
    def await_review(self, reviewer_notes: str, priority: str):
        # ... step logic ...
    """

    def decorator(step_func):
        # Mark the function so the engine knows to suspend when transitioning to it
        step_func._has_statezero_action = True

        # Get the step function signature to build the action signature
        step_sig = inspect.signature(step_func)
        step_params = list(step_sig.parameters.values())

        # Remove 'self' parameter for the action signature
        action_params = [p for p in step_params if p.name != "self"]

        # Add workflow_run_id as the first parameter
        workflow_run_id_param = inspect.Parameter(
            "workflow_run_id", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=int
        )

        action_params.insert(0, workflow_run_id_param)

        # Create new signature for the action
        action_sig = step_sig.replace(parameters=action_params)

        # Handle input serializer - add workflow_run_id if provided
        final_serializer = None
        if serializer:
            # Check if workflow_run_id is already in the serializer
            serializer_instance = serializer()
            if "workflow_run_id" not in serializer_instance.fields:
                # Add workflow_run_id as the FIRST field to match function signature order
                new_declared_fields = {"workflow_run_id": serializers.IntegerField()}
                new_declared_fields.update(serializer._declared_fields)
                serializer._declared_fields = new_declared_fields
            final_serializer = serializer

        # Handle response serializer - use provided or create default
        final_response_serializer = response_serializer
        if final_response_serializer is None:

            class DefaultResponseSerializer(serializers.Serializer):
                status = serializers.CharField()
                message = serializers.CharField()
                workflow_status = serializers.CharField()
                current_step = serializers.CharField()

            final_response_serializer = DefaultResponseSerializer

        # Create the action function
        def action_function(workflow_run_id: int, **kwargs):
            """Generated action that calls the workflow step"""
            logger.debug(
                f"Action {step_func.__name__} called with workflow_run_id={workflow_run_id}, kwargs={kwargs}"
            )

            # Get workflow run - will raise Http404 if not found
            workflow_run = get_object_or_404(WorkflowRun, id=workflow_run_id)

            # --- Validate that the workflow is suspended and waiting for this action ---
            if workflow_run.status != WorkflowStatus.SUSPENDED:
                raise DRFValidationError(
                    f"Workflow is not in a state to receive this action. Current status: {workflow_run.status}"
                )

            # Validate workflow exists in registry
            if workflow_run.name not in _workflows:
                logger.error(f"Workflow {workflow_run.name} not found in registry")
                raise ValidationError(f"Workflow {workflow_run.name} not registered")

            # Set up workflow context
            workflow_cls = _workflows[workflow_run.name]
            ctx_manager = WorkflowContextManager(workflow_run_id, workflow_cls.Context)
            token = _current_context.set(ctx_manager)

            try:
                # Create workflow instance and call the step method
                workflow_instance = workflow_cls()

                # Verify step method exists
                if not hasattr(workflow_instance, step_func.__name__):
                    logger.error(
                        f"Step method {step_func.__name__} not found on workflow {workflow_run.name}"
                    )
                    raise ValidationError(
                        f"Step {step_func.__name__} not found on workflow"
                    )

                step_method = getattr(workflow_instance, step_func.__name__)

                # Get the step function's expected parameters (excluding 'self')
                step_sig = inspect.signature(step_method)
                expected_params = {
                    name
                    for name, param in step_sig.parameters.items()
                    if name != "self"
                }

                # Filter kwargs to only include parameters the step method expects
                # This includes 'request' if the step method expects it
                filtered_kwargs = {
                    key: value
                    for key, value in kwargs.items()
                    if key in expected_params
                }

                # Call step with filtered arguments
                result = step_method(**filtered_kwargs)

                # Commit context changes
                ctx_manager.commit_changes()

                # Handle the step result through workflow engine
                if result is not None:
                    engine._handle_result(workflow_run, result)

                workflow_run.refresh_from_db()

                logger.debug(f"Step {step_func.__name__} executed successfully")
                return {
                    "status": "success",
                    "message": f"Step {step_func.__name__} executed",
                    "workflow_status": str(workflow_run.status),
                    "current_step": workflow_run.current_step,
                }

            finally:
                _current_context.reset(token)

        # Extract workflow class name from the method's qualified name
        # e.g., "ExpenseWorkflow.await_review" -> "ExpenseWorkflow"
        # e.g., "test_func.<locals>.StepDisplayWorkflow.collect_info" -> "StepDisplayWorkflow"
        # We need the immediate parent (the class name), not the full path
        qualname_parts = step_func.__qualname__.split('.')
        # The method name is the last part, the class name is the second to last
        class_name = qualname_parts[-2] if len(qualname_parts) >= 2 else ''

        # Build action name: workflow_{ClassName}_{action_name}
        action_name = name or step_func.__name__
        full_action_name = f"workflow_{class_name}_{action_name}" if class_name else f"workflow_{action_name}"

        # Set the signature and metadata for the action function
        action_function.__signature__ = action_sig
        action_function.__name__ = full_action_name
        action_function.__doc__ = step_func.__doc__

        # Apply the StateZero @action decorator
        try:
            decorated_action = action(
                name=full_action_name,
                serializer=final_serializer,
                response_serializer=final_response_serializer,
                permissions=permissions or [],
                display=display,
            )(action_function)
        except Exception as e:
            logger.exception(f"Error decorating action {step_func.__name__}: {str(e)}")
            raise

        step_func._action_function = decorated_action
        step_func._full_action_name = full_action_name  # Store for frontend

        return step_func

    return decorator
