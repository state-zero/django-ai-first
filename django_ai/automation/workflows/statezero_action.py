# django_ai/automation/workflows/statezero_action.py

import inspect
import logging
from rest_framework import serializers
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
from .models import WorkflowRun

logger = logging.getLogger(__name__)


def statezero_action(name=None, serializer=None, response_serializer=None, permissions=None):
    """
    Decorator that makes a workflow step callable as a StateZero action.

    Automatically handles workflow_run_id and calls the step with remaining arguments.

    Usage:
    @statezero_action(name="expense_submit_review", serializer=ReviewInputSerializer)
    @step()
    def await_review(self, reviewer_notes: str, priority: str):
        # workflow_run_id is handled automatically
        # step just gets the business logic arguments
    """

    def decorator(step_func):
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
                # Add workflow_run_id field to existing serializer
                serializer._declared_fields = serializer._declared_fields.copy()
                serializer._declared_fields["workflow_run_id"] = (
                    serializers.IntegerField()
                )
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

        # Set the signature and metadata for the action function
        action_function.__signature__ = action_sig
        action_function.__name__ = name or f"workflow_{step_func.__name__}"
        action_function.__doc__ = step_func.__doc__

        # Apply the StateZero @action decorator
        try:
            decorated_action = action(
                name=name or f"workflow_{step_func.__name__}",
                serializer=final_serializer,
                response_serializer=final_response_serializer,
                permissions=permissions or [],
            )(action_function)
        except Exception as e:
            logger.exception(f"Error decorating action {step_func.__name__}: {str(e)}")
            raise

        # Mark the original step function so we know it has an action
        step_func._hstatezero_action = True
        step_func._action_function = decorated_action

        return step_func

    return decorator