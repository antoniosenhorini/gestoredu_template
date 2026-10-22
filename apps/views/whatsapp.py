from .utilitarios import *

# ===== WEBHOOK PARA WHATSAPP =====
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse, JsonResponse
from django.utils import timezone

@csrf_exempt
def webhook_whatsapp(request):
    """Recebe respostas dos pais via WhatsApp"""
    if request.method == 'POST':
        dados = request.POST
        numero_pai = dados.get('From', '').replace('whatsapp:', '')
        mensagem_texto = dados.get('Body', '')

        from ..models import Aluno, RegistroOcorrenciaAluno

        # Buscar aluno pelo telefone
        aluno = Aluno.objects.filter(telefone=numero_pai).first()

        if aluno:
            # Buscar a falta mais recente sem resposta
            ocorrencia = RegistroOcorrenciaAluno.objects.filter(
                aluno=aluno,
                faltou=True,
                resposta_disparo__isnull=True
            ).order_by('-data').first()

            if ocorrencia:
                # Salvar a resposta
                ocorrencia.resposta_disparo = mensagem_texto
                ocorrencia.resposta_disparo_data = timezone.now()
                ocorrencia.save()

                print(f"✅ Resposta WhatsApp salva para {aluno.nome}")

        return HttpResponse('OK', status=200)

    return JsonResponse({'erro': 'Método não permitido'}, status=405)
