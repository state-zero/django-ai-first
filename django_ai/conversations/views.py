from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status
import pusher

from .models import ConversationSession
from django_ai.conf import get_pusher_config


@api_view(['POST'])
@permission_classes([AllowAny])  # We handle auth manually based on session ownership
def pusher_auth(request):
    """
    Authenticate Pusher private channel subscriptions.
    Ensures users can only subscribe to their own conversation sessions.
    """
    socket_id = request.data.get('socket_id')
    channel_name = request.data.get('channel_name')
    
    if not socket_id or not channel_name:
        return Response(
            {'error': 'Missing socket_id or channel_name'}, 
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Extract session_id from channel name
    # Format: "private-conversation-session-{session_id}"
    if not channel_name.startswith('private-conversation-session-'):
        return Response(
            {'error': 'Invalid channel name'}, 
            status=status.HTTP_403_FORBIDDEN
        )
    
    session_id = channel_name.replace('private-conversation-session-', '')
    
    # Verify the user has access to this session
    try:
        session = ConversationSession.objects.get(id=session_id)
        
        # Check if user owns this session
        if request.user.is_authenticated:
            # Authenticated users: must own the session
            if session.user and session.user != request.user:
                return Response(
                    {'error': 'Unauthorized access to this conversation'}, 
                    status=status.HTTP_403_FORBIDDEN
                )
        else:
            # Anonymous users: check anonymous_id from session or request data
            # First try Django session
            anonymous_id = request.session.get('anonymous_id')
            
            # Fallback to request data if needed (for stateless clients)
            if not anonymous_id:
                anonymous_id = request.data.get('anonymous_id')
            
            if not session.anonymous_id or session.anonymous_id != anonymous_id:
                return Response(
                    {'error': 'Unauthorized access to this conversation'}, 
                    status=status.HTTP_403_FORBIDDEN
                )
    
    except ConversationSession.DoesNotExist:
        return Response(
            {'error': 'Conversation session not found'}, 
            status=status.HTTP_404_NOT_FOUND
        )
    
    # Initialize Pusher with DJANGO_AI['PUSHER'] config
    pusher_config = get_pusher_config()
    
    if not pusher_config.get('KEY'):
        return Response(
            {'error': 'Pusher not configured'}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    
    pusher_client = pusher.Pusher(
        app_id=pusher_config.get('APP_ID'),
        key=pusher_config.get('KEY'),
        secret=pusher_config.get('SECRET'),
        cluster=pusher_config.get('CLUSTER', 'us2'),
        ssl=True
    )
    
    # Authenticate the channel subscription
    try:
        auth = pusher_client.authenticate(
            channel=channel_name,
            socket_id=socket_id
        )
        return Response(auth)
    except Exception as e:
        return Response(
            {'error': f'Pusher authentication failed: {str(e)}'}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )