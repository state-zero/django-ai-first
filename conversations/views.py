from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
import json
from .service import ConversationService


@csrf_exempt
@require_http_methods(["POST"])
def send_message(request):
    """API endpoint for sending chat messages"""
    try:
        data = json.loads(request.body)

        result = ConversationService.send_message(
            session_id=data["session_id"],
            message=data["message"],
            user=request.user if request.user.is_authenticated else None,
        )

        return JsonResponse(result)

    except Exception as e:
        return JsonResponse({"status": "error", "error": str(e)}, status=400)


@csrf_exempt
@require_http_methods(["POST"])
def create_session(request):
    """Create new chat session"""
    try:
        data = json.loads(request.body)

        session = ConversationService.create_session(
            agent_path=data["agent_path"],
            user=request.user if request.user.is_authenticated else None,
            anonymous_id=data.get("anonymous_id"),
            context=data.get("context", {}),
        )

        return JsonResponse(
            {
                "status": "success",
                "session_id": str(session.id),
                "agent_path": session.agent_path,
            }
        )

    except Exception as e:
        return JsonResponse({"status": "error", "error": str(e)}, status=400)
