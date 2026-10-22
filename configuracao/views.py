from django.shortcuts import render, redirect
from django.contrib import messages
from .models import ConfiguracaoEscola
from .forms import ConfiguracaoEscolaForm

def setup(request):
    escola = ConfiguracaoEscola.get_solo()
    if escola.setup_concluido:
        return redirect("home")
    if request.method == "POST":
        form = ConfiguracaoEscolaForm(request.POST, request.FILES, instance=escola)
        if form.is_valid():
            config = form.save(commit=False)
            config.setup_concluido = True
            config.save()
            messages.success(request, "Configuracao concluida com sucesso!")
            return redirect("home")
    else:
        form = ConfiguracaoEscolaForm(instance=escola)
    return render(request, "configuracao/setup.html", {"form": form})
