# middleware.py
from django.shortcuts import redirect
from django.contrib import messages
from .models import UserPortfolio

class FrozenAccountMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            try:
                portfolio = UserPortfolio.objects.get(user=request.user)
                if portfolio.is_frozen:
                    # Allow access only to logout and suspended account page
                    allowed_paths = ['/accounts/logout/', '/suspended/', '/admin/']
                    if not any(request.path.startswith(path) for path in allowed_paths):
                        return redirect('suspended_account')
            except UserPortfolio.DoesNotExist:
                pass
        
        response = self.get_response(request)
        return response