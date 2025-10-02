from django.urls import path
from . import views

app_name = 'django_ai_conversations'

urlpatterns = [
    path('pusher/auth/', views.pusher_auth, name='pusher_auth'),
]