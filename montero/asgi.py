import os
import django
from django.core.asgi import get_asgi_application

# Set the default Django settings module
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'montero.settings')

# Setup Django BEFORE importing any models or middleware
django.setup()

# Now we can safely import Django-dependent modules
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from crypto_app.middleware import TokenAuthMiddlewareStack
import crypto_app.routing

# Get the Django ASGI application
django_asgi_app = get_asgi_application()

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": TokenAuthMiddlewareStack(
        URLRouter(
            crypto_app.routing.websocket_urlpatterns
        )
    ),
})