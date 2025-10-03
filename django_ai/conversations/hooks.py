"""
StateZero hooks with request passing.
"""


def set_message_type(data, request=None):
    """Pre-hook to set message type"""
    data["message_type"] = "user"
    return data


def set_processing_status(data, request=None):
    """Pre-hook to set processing status"""
    if data.get("message_type") == "user":
        data["processing_status"] = "pending"
    return data

def set_user_context(data, request=None):
    """Pre-hook to set user context on sessions"""
    if request and hasattr(request, "user") and request.user.is_authenticated:
        data["user"] = request.user.id
    return data