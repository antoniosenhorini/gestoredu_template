from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.contrib.auth.models import User, Group
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.conf import settings
from django.utils import timezone
from django.db.models import Count, Q  # se for usar no gráfico
from django.db import IntegrityError

import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill
import os
from datetime import datetime
import pandas as pd          # <--- NOVA LINHA
from io import BytesIO

from ..models import (
    Evento, Documento, Recado, DocumentoPrivado, EventoPrivado, RecadoInterno,
    PeriodoAula, RegistroFalta, Video, Turma, Aluno, RegistroFaltaAluno,
    RegistroOcorrenciaAluno, LogLogin, Professor  # ← ADICIONE APENAS Professor
)

from ..forms import (
    RecadoInternoForm, DocumentoPrivadoForm, EventoPrivadoForm,
    RegistroOcorrenciaForm, TIPOS_OCORRENCIA_CHOICES_PERMITIDOS,
    TIPOS_OCORRENCIA_LEGADOS_FALTA, TIPOS_OCORRENCIA_PERMITIDOS,
    PEDAGOGA_NAO_DEFINIDA, normalizar_tipo_ocorrencia, normalizar_turma_mapeamento,
    pedagoga_por_turma, resolver_pedagoga_turma
)

# ===== NOVO IMPORT DO WHATSAPP =====
from apps.utils import enviar_whatsapp
from ..permissions import (
    faltas_required,
    avisos_professores_required,
    is_consulta_cronograma,
    is_digitador,
    is_equipe_diretiva,
    is_professor,
    is_superuser,
    justificar_faltas_required,
    ocorrencias_required,
    painel_required,
    pode_acessar_faltas,
    pode_acessar_ocorrencias,
    pode_acessar_painel,
    pode_acessar_relatorios,
    pode_acessar_avisos_professores,
    pode_consultar_cronograma_laboratorios,
    pode_justificar_faltas,
    relatorios_required,
)

# Função de teste para verificar se o usuário está no grupo "Equipe Diretiva"
def pertence_ao_grupo_equipe_diretiva(user):
    return is_superuser(user) or is_equipe_diretiva(user)

# Consulta somente leitura do cronograma de laboratorios: Equipe Diretiva e
# superuser (que ja podem tudo) mais o grupo "Consulta Cronograma" (so
# leitura). NUNCA usar esta funcao para proteger uma acao de escrita -
# para isso continue usando pertence_ao_grupo_equipe_diretiva.
def pode_ver_cronograma_laboratorios(user):
    return pode_consultar_cronograma_laboratorios(user)

# ===== FUNÇÕES PARA DIGITADORES =====
def grupo_digitadores(user):
    """Verifica se o usuário pertence ao grupo Digitadores"""
    return is_digitador(user)
# ===== REDIRECIONAMENTO PERSONALIZADO PARA LOGIN =====
from django.contrib.auth.views import LoginView

class CustomLoginView(LoginView):
    def get_success_url(self):
        user = self.request.user

        # Registra o acesso no log
        from ..models import LogLogin
        LogLogin.objects.create(
            usuario=user,
            ip=self.request.META.get('REMOTE_ADDR'),
            user_agent=self.request.META.get('HTTP_USER_AGENT', '')
        )

        if pode_acessar_painel(user):
            return '/painel-equipe/'
        if is_digitador(user):
            return '/faltas-alunos/registrar/'
        return '/'


from django.utils import timezone
from datetime import datetime
from calendar import monthrange

def pertence_ao_grupo_equipe_ou_digitadores(user):
    return is_superuser(user) or is_equipe_diretiva(user) or is_digitador(user)
