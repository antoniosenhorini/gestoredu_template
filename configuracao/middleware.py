from django.shortcuts import redirect
from .models import ConfiguracaoEscola

class SetupMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/setup/") or request.path.startswith("/admin/") or request.path.startswith("/static/"):
            return self.get_response(request)
        escola = ConfiguracaoEscola.get_solo()
        if not escola.setup_concluido:
            return redirect("setup")
        return self.get_response(request)
