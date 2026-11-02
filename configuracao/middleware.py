from django.shortcuts import redirect
from django.utils import timezone
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


# --- NOVO MIDDLEWARE ADICIONADO AQUI ---
class TrialMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Caminhos que NUNCA devem ser bloqueados
        caminhos_livres = (
            '/expirado/',
            '/login/',
            '/logout/',
            '/admin/',
            '/static/',
            '/media/',
            '/setup/',
        )

        if request.path.startswith(caminhos_livres):
            return self.get_response(request)

        # Superusuário nunca é bloqueado
        if request.user.is_authenticated and request.user.is_superuser:
            return self.get_response(request)

        # Verifica a data de expiração
        try:
            config = ConfiguracaoEscola.objects.first()
            if config and config.data_expiracao:
                if timezone.now().date() > config.data_expiracao:
                    return redirect('trial_expirado')
        except Exception:
            pass

        return self.get_response(request)

