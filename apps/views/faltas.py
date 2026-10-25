import logging

from .utilitarios import *
from django.db import transaction
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)

# Mensagens atualizadas para Notificação Diária
MENSAGEM_FALTA_DIARIA = (
    '✅ Falta registrada com sucesso. O responsável será notificado via WhatsApp.'
)
MENSAGEM_OCORRENCIA_DIARIA = (
    '✅ Ocorrência registrada com sucesso. O responsável será notificado via WhatsApp.'
)

TIPOS_OCORRENCIA_WHATSAPP = {'atraso', 'piercing', 'desvio_normas', 'cabelo', 'uniforme', 'fora_sala', 'matando_aula'}
TIPOS_OCORRENCIA_REINCIDENCIA = {'piercing', 'desvio_normas', 'cabelo', 'uniforme'}
TIPOS_OCORRENCIA_LABELS = {
    'atraso': 'Atraso',
    'piercing': 'Uso de Piercing',
    'desvio_normas': 'Desvio de Normas',
    'cabelo': 'Cabelo',
    'uniforme': 'Uniforme',
    'fora_sala': 'Fora da sala',
    'matando_aula': 'Matando aula',
}


def _telefone_whatsapp_aluno(aluno):
    telefone = getattr(aluno, 'telefone', None)
    return str(telefone).strip() if telefone else ''


def _data_ocorrencia_whatsapp(data_ocorrencia):
    try:
        return datetime.strptime(str(data_ocorrencia), '%Y-%m-%d').strftime('%d/%m/%Y')
    except ValueError:
        return str(data_ocorrencia)


def _tipo_ocorrencia_whatsapp(tipo_ocorrencia):
    return TIPOS_OCORRENCIA_LABELS.get(tipo_ocorrencia, tipo_ocorrencia)


def _enviar_aviso_ocorrencia_whatsapp(request, aluno, tipo_ocorrencia, data_ocorrencia, mensagem_sucesso=None):
    messages.success(request, mensagem_sucesso or MENSAGEM_OCORRENCIA_DIARIA)


# ===== NOVAS VIEWS PARA CONTROLE DE FALTAS =====

@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def controle_faltas(request):
    professores = Professor.objects.all().order_by('nome_completo')

    # Captura e converte os valores da URL
    mes_str = request.GET.get('mes')
    ano_str = request.GET.get('ano')

    if mes_str:
        mes = int(mes_str)
    else:
        mes = timezone.now().month

    if ano_str:
        ano = int(ano_str)
    else:
        ano = timezone.now().year

    faltas = RegistroFalta.objects.filter(data__month=mes, data__year=ano).select_related('professor').order_by('-data')
    periodos = PeriodoAula.objects.all().order_by('ordem')
    resumo = []
    
    for professor in professores:
        faltas_prof = [f for f in faltas if f.professor.id == professor.id]
        total_minutos = sum(f.minutos_faltados() for f in faltas_prof)
        total_faltas = len([f for f in faltas_prof if f.tipo == 'falta'])
        total_atrasos = len([f for f in faltas_prof if f.tipo == 'atraso'])
        resumo.append({
            'nome': professor.nome_completo, # <--- Agora puxa o nome direto da tabela Professor
            'total_minutos': total_minutos,
            'total_horas': round(total_minutos / 60, 1),
            'total_faltas': total_faltas,
            'total_atrasos': total_atrasos,
        })
        
    resumo = sorted(resumo, key=lambda x: x['nome'])
    context = {
        'professores': professores,
        'faltas': faltas,
        'periodos': periodos,
        'resumo': resumo,
        'mes': mes,
        'ano': ano,
        'meses': range(1, 13),
        'anos': [2026, 2027],
    }
    return render(request, 'faltas/controle_faltas.html', context)

@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def registrar_falta(request):
    professores = Professor.objects.all().order_by('nome_completo')
    if request.method == 'GET':
        return render(request, 'faltas/registrar_falta.html', {'professores': professores})
    if request.method == 'POST':
        professor_id = request.POST.get('professor')
        data = request.POST.get('data')
        horario_previsto = request.POST.get('horario_previsto')
        horario_real = request.POST.get('horario_real') or None
        tipo = request.POST.get('tipo')
        observacao = request.POST.get('observacao', '')
        try:
            professor = Professor.objects.get(id=professor_id)
            data_obj = datetime.strptime(data, '%Y-%m-%d').date()
            previsto_obj = datetime.strptime(horario_previsto, '%H:%M').time()
            real_obj = None
            if horario_real:
                real_obj = datetime.strptime(horario_real, '%H:%M').time()
            minutos_atraso = 0
            if real_obj and tipo == 'atraso':
                previsto_min = previsto_obj.hour * 60 + previsto_obj.minute
                real_min = real_obj.hour * 60 + real_obj.minute
                minutos_atraso = max(0, real_min - previsto_min)
            RegistroFalta.objects.create(
                professor=professor,
                data=data_obj,
                horario_previsto=previsto_obj,
                horario_real=real_obj,
                tipo=tipo,
                minutos_atraso=minutos_atraso,
                observacao=observacao,
                registrado_por=request.user
            )
            messages.success(request, 'Registro salvo com sucesso!')
            return redirect('controle_faltas')
        except User.DoesNotExist:
            messages.error(request, 'Professor nÃƒÂ£o encontrado!')
        except Exception as e:
            messages.error(request, f'Erro ao salvar: {str(e)}')
        return redirect('controle_faltas')

# =============================================================================
# CONTROLE DE FALTAS - ALUNOS (LISTAGEM)
# =============================================================================
from django.utils import timezone

@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def controle_faltas_alunos(request):
    # Filtros
    mes = request.GET.get('mes', timezone.now().month)
    ano = request.GET.get('ano', timezone.now().year)

    # Busca ocorrÃƒÂªncias do perÃƒÂ­odo
    faltas = RegistroFaltaAluno.objects.filter(
        data__month=mes,
        data__year=ano
    ).select_related('aluno', 'aluno__turma').order_by('-data', 'aluno__turma__nome', 'aluno__numero')

    context = {
        'faltas': faltas,
        'mes': mes,
        'ano': ano,
        'meses': range(1, 13),
        'anos': [2026, 2027, 2028],
    }
    return render(request, 'faltas/controle_faltas_alunos.html', context)

# =============================================================================
# EDITAR OCORRÃƒÅ NCIA DE ALUNO
# =============================================================================

@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def editar_ocorrencia_aluno(request, pk):
    from datetime import datetime
    ocorrencia = get_object_or_404(RegistroOcorrenciaAluno, id=pk)

    if request.method == 'POST':

        # Salva os campos da busca ativa.
        responsavel = request.POST.get('responsavel_contatado', '').strip()
        alegado = request.POST.get('alegado_responsavel', '').strip()
        horario_raw = request.POST.get('horario_contato', '').strip()

        ocorrencia.responsavel_contatado = responsavel
        ocorrencia.alegado_responsavel = alegado

        if horario_raw:
            try:
                if ':' in horario_raw:
                    partes = horario_raw.split(':')
                    hora = int(partes[0])
                    minuto = int(partes[1])
                    ocorrencia.horario_contato = datetime.now().replace(hour=hora, minute=minuto, second=0, microsecond=0).time()
                else:
                    ocorrencia.horario_contato = None
            except Exception as e:
                print(f"Erro na conversao do horario: {e}")
                ocorrencia.horario_contato = None
        else:
            ocorrencia.horario_contato = None

        ocorrencia.save()

        # Marca automaticamente como busca ativa realizada.
        ocorrencia.busca_ativa_realizada = True
        ocorrencia.save(update_fields=['busca_ativa_realizada'])

        # 2. Processa o restante do formulÃƒÂ¡rio
        form = RegistroOcorrenciaForm(request.POST, instance=ocorrencia)
        if form.is_valid():
            form.save()
        else:
            print("Erros do formulario:", form.errors)
            # Tenta salvar pelo menos os campos importantes que vieram no POST
            if 'atendido_por' in request.POST:
                ocorrencia.atendido_por = request.POST.get('atendido_por', '')
            if 'motivo_alegado' in request.POST:
                ocorrencia.motivo_alegado = request.POST.get('motivo_alegado', '')
            ocorrencia.save(update_fields=['atendido_por', 'motivo_alegado'])
            messages.warning(request, 'Busca Ativa salva, mas outros dados apresentaram erro. Verifique o formulario.')

        # Redireciona de volta para a Busca Ativa
        url = '/busca-ativa/'
        params = []
        if request.GET.get('mes'):
            params.append(f'mes={request.GET.get("mes")}')
        if request.GET.get('ano'):
            params.append(f'ano={request.GET.get("ano")}')
        if request.GET.get('turma'):
            params.append(f'turma={request.GET.get("turma")}')
        if params:
            url += '?' + '&'.join(params)
        return redirect(url)

    else:
        # GET: mostra o formulario
        form = RegistroOcorrenciaForm(instance=ocorrencia)
        form.fields['turma'].initial = ocorrencia.aluno.turma
        form.fields['numero_aluno'].initial = ocorrencia.aluno.numero
        form.fields['nome_aluno'].initial = ocorrencia.aluno.nome
        form.fields['data'].initial = ocorrencia.data

    return render(request, 'ocorrencias/editar_ocorrencia_aluno.html', {
        'form': form,
        'ocorrencia': ocorrencia,
    })

# =============================================================================
# EXCLUIR OCORRÃƒÅ NCIA DE ALUNO
# =============================================================================
@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def excluir_ocorrencia_aluno(request, pk):
    ocorrencia = get_object_or_404(RegistroOcorrenciaAluno, id=pk)
    if request.method == 'POST':
        ocorrencia.delete()
        messages.success(request, 'Ocorrencia excluida com sucesso!')
        return redirect('controle_faltas_alunos')
    return render(request, 'ocorrencias/confirmar_exclusao_ocorrencia.html', {'ocorrencia': ocorrencia})


# =============================================================================
# VIEW PARA EDITAR UM REGISTRO DE FALTA
# =============================================================================
@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def editar_falta(request, falta_id):
    falta = get_object_or_404(RegistroFalta, id=falta_id)
    professores = Professor.objects.all().order_by('nome_completo')

    if request.method == 'POST':
        professor_id = request.POST.get('professor')
        data = request.POST.get('data')
        horario_previsto = request.POST.get('horario_previsto')
        horario_real = request.POST.get('horario_real') or None
        tipo = request.POST.get('tipo')
        observacao = request.POST.get('observacao', '')

        try:
            professor = Professor.objects.get(id=professor_id)
            falta.professor = professor
            falta.data = datetime.strptime(data, '%Y-%m-%d').date()
            falta.horario_previsto = datetime.strptime(horario_previsto, '%H:%M').time()
            falta.horario_real = datetime.strptime(horario_real, '%H:%M').time() if horario_real else None
            falta.tipo = tipo
            falta.observacao = observacao

            # Recalcular minutos de atraso
            if falta.horario_real and tipo == 'atraso':
                previsto_min = falta.horario_previsto.hour * 60 + falta.horario_previsto.minute
                real_min = falta.horario_real.hour * 60 + falta.horario_real.minute
                falta.minutos_atraso = max(0, real_min - previsto_min)
            else:
                falta.minutos_atraso = 0

            falta.save()
            messages.success(request, 'Registro atualizado com sucesso!')
            return redirect('controle_faltas')
        except Exception as e:
            messages.error(request, f'Erro ao atualizar: {str(e)}')
            return redirect('editar_falta', falta_id=falta.id)

    # GET - exibe o formulÃƒÂ¡rio
    context = {
        'falta': falta,
        'professores': professores,
        'hoje': falta.data.strftime('%Y-%m-%d'),
        'horario_previsto': falta.horario_previsto.strftime('%H:%M') if falta.horario_previsto else '',
        'horario_real': falta.horario_real.strftime('%H:%M') if falta.horario_real else '',
    }
    return render(request, 'faltas/editar_falta.html', context)
# =============================================================================
# VIEWS PARA EXCLUSÃƒÆ’O DE REGISTROS DE FALTA
# =============================================================================

@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def excluir_falta(request, falta_id):
    """Exclui um ÃƒÂºnico registro de falta."""
    falta = get_object_or_404(RegistroFalta, id=falta_id)
    falta.delete()
    messages.success(request, 'Registro excluÃƒÂ­do com sucesso.')
    return redirect('controle_faltas')


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def excluir_faltas_selecionadas(request):
    """Exclui mÃƒÂºltiplos registros de falta enviados por POST."""
    if request.method == 'POST':
        ids = request.POST.getlist('ids')
        if ids:
            RegistroFalta.objects.filter(id__in=ids).delete()
            messages.success(request, f'{len(ids)} registro(s) excluÃƒÂ­do(s) com sucesso.')
        else:
            messages.warning(request, 'Nenhum registro selecionado para exclusÃƒÂ£o.')
    return redirect('controle_faltas')

# =============================================================================
# CONTROLE DE FALTAS - ALUNOS
# =============================================================================
from ..models import Turma, Aluno, RegistroFaltaAluno, AtestadoAluno

@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def controle_faltas_alunos_antigo(request):
    turmas = Turma.objects.filter(ativa=True).order_by('nome_completo')
    mes = request.GET.get('mes', timezone.now().month)
    ano = request.GET.get('ano', timezone.now().year)

    faltas = RegistroFaltaAluno.objects.filter(
        data__month=mes,
        data__year=ano
    ).select_related('aluno', 'aluno__turma').order_by('-data')

    context = {
        'turmas': turmas,
        'faltas': faltas,
        'mes': mes,
        'ano': ano,
        'meses': range(1, 13),
        'anos': [2026, 2027, 2028],
    }
    return render(request, 'faltas/controle_faltas_alunos.html', context)

@faltas_required
def registrar_falta_aluno(request):
    if request.method == 'POST':
        # Mantem a ultima selecao mesmo quando o fluxo retorna antes do final.
        turno_selecionado = request.POST.get('turno_opcao', '').strip()
        turma_selecionada = request.POST.get('turma', '').strip()

        if turno_selecionado:
            request.session['ultimo_turno_falta_aluno'] = turno_selecionado
        if turma_selecionada:
            request.session['ultima_turma_falta_aluno'] = turma_selecionada

        aluno_id = request.POST.get('aluno')
        data = request.POST.get('data')
        quantidade = request.POST.get('quantidade', 1)
        possui_atestado = request.POST.get('possui_atestado') == 'sim'
        justificada = possui_atestado or request.POST.get('justificada') == 'on'
        responsavel = request.POST.get('responsavel', '')
        observacoes = request.POST.get('observacoes', '')
        pedagoga = request.POST.get('pedagoga', '').strip()

        try:
            aluno = Aluno.objects.get(id=aluno_id)
            # Força o turno integral para não dar erro no banco
            request.session['ultimo_turno_falta_aluno'] = 'integral'
            request.session['ultima_turma_falta_aluno'] = str(aluno.turma_id)
            request.session.modified = True

            aluno_turma_msg = f'Aluno: {aluno.nome} - Turma: {aluno.turma}.'
            logger.info('Turma normalizada para mapeamento de pedagoga: %s', normalizar_turma_mapeamento(aluno.turma))
            pedagoga = resolver_pedagoga_turma(aluno.turma, pedagoga)

            # Converte a data para objeto date real (ESSENCIAL para as travas)
            data_falta_obj = datetime.strptime(data, '%Y-%m-%d').date()

            atestado_ativo = AtestadoAluno.objects.filter(
                aluno=aluno,
                ativo=True,
                data_inicio__lte=data_falta_obj,
                data_fim__gte=data_falta_obj,
            ).first()

            if atestado_ativo:
                dias_restantes = max((atestado_ativo.data_fim - data_falta_obj).days, 0)
                messages.warning(request, f'⚠️ Aluno em atestado. Faltam {dias_restantes} dias para o término do atestado.')
                return redirect('registrar_falta_aluno')

            # TRAVA 1: Já existe falta no mesmo dia? (usando data_falta_obj)
            if RegistroFaltaAluno.objects.filter(aluno=aluno, data=data_falta_obj).exists():
                messages.warning(request, f'Este aluno já possui FALTA registrada nesta data. {aluno_turma_msg}')
                return redirect('registrar_falta_aluno')

            # TRAVA 2: Já existe um atraso no mesmo dia? (usando data_falta_obj)
            if RegistroOcorrenciaAluno.objects.filter(
                aluno=aluno,
                data=data_falta_obj,
                tipo_ocorrencia='atraso'
            ).exists():
                messages.error(
                    request,
                    f'⛔ BLOQUEADO: Este aluno já foi registrado como ATRASADO hoje. '
                    f'Ele está presente! {aluno_turma_msg}'
                )
                return redirect('registrar_falta_aluno')

            # Registra falta (usando data_falta_obj)
            falta = RegistroFaltaAluno.objects.create(
                aluno=aluno,
                data=data_falta_obj,
                quantidade_faltas=quantidade,
                justificada=justificada,
                responsavel_contatado=responsavel,
                observacoes=observacoes,
                pedagoga=pedagoga,
                registrado_por=request.user
            )

            if possui_atestado:
                messages.success(request, f'{MENSAGEM_FALTA_DIARIA} {aluno_turma_msg}')
                return redirect('registrar_falta_aluno')

            messages.success(request, f'{MENSAGEM_FALTA_DIARIA} {aluno_turma_msg}')

        except Exception as e:
            messages.error(request, f'Erro real: {str(e)}')

        return redirect('registrar_falta_aluno')

    # GET - exibe formulário
    turmas = Turma.objects.filter(ativa=True).order_by('turno', 'nome')
    alunos = Aluno.objects.filter(ativo=True).select_related('turma').order_by('turma__nome', 'numero')

    ultimo_turno = request.session.get('ultimo_turno_falta_aluno', 'integral')
    ultima_turma_id = request.session.get('ultima_turma_falta_aluno', '')

    contexto = {
        'turmas': [
            {
                'id': turma.id,
                'nome': turma.nome,
                'serie': turma.serie,
                'turno': turma.turno or 'integral',
                'label': str(turma),
            }
            for turma in turmas
        ],
        'alunos': [
            {
                'id': aluno.id,
                'nome': aluno.nome,
                'numero': aluno.numero,
                'turma_id': aluno.turma_id,
                'turma_nome': aluno.turma.nome,
                'turma_serie': aluno.turma.serie,
                'turma_label': str(aluno.turma),
                'turno': aluno.turma.turno or 'integral',
                'pedagoga': pedagoga_por_turma(aluno.turma),
            }
            for aluno in alunos
        ],
        'pedagoga_nao_definida': PEDAGOGA_NAO_DEFINIDA,
        'ultimo_turno_falta_aluno': ultimo_turno,
        'ultima_turma_falta_aluno': ultima_turma_id,
        'ultima_turma_id': ultima_turma_id,
    }
    return render(request, 'faltas/registrar_falta_aluno.html', contexto)

def _contexto_conferencia_faltas_alunos(request):
    data_filtro = request.GET.get('data') or timezone.now().date().isoformat()
    turno_filtro = request.GET.get('turno', '')
    turma_filtro = request.GET.get('turma', '')
    pedagoga_filtro = request.GET.get('pedagoga', '')

    faltas = RegistroFaltaAluno.objects.filter(data=data_filtro).select_related('aluno', 'aluno__turma')

    if turno_filtro:
        faltas = faltas.filter(aluno__turma__turno=turno_filtro)
    if turma_filtro:
        faltas = faltas.filter(aluno__turma_id=turma_filtro)
    if pedagoga_filtro:
        faltas = faltas.filter(pedagoga=pedagoga_filtro)

    faltas = faltas.order_by('aluno__turma__turno', 'aluno__turma__nome', 'aluno__numero')
    turmas = Turma.objects.filter(ativa=True).order_by('turno', 'nome')
    pedagogas = (
        RegistroFaltaAluno.objects.exclude(pedagoga='')
        .values_list('pedagoga', flat=True)
        .distinct()
        .order_by('pedagoga')
    )

    return {
        'faltas': faltas,
        'turmas': turmas,
        'pedagogas': pedagogas,
        'data_filtro': data_filtro,
        'turno_filtro': turno_filtro,
        'turma_filtro': turma_filtro,
        'pedagoga_filtro': pedagoga_filtro,
    }


@faltas_required
def conferencia_faltas_alunos(request):
    contexto = _contexto_conferencia_faltas_alunos(request)
    return render(request, 'faltas/conferencia_faltas_alunos.html', contexto)


@faltas_required
def imprimir_conferencia_faltas_alunos(request):
    contexto = _contexto_conferencia_faltas_alunos(request)
    contexto['impresso_em'] = timezone.now()
    return render(request, 'faltas/conferencia_faltas_alunos_print.html', contexto)


def _contexto_conferencia_ocorrencias(request):
    data_filtro = request.GET.get('data') or timezone.now().date().isoformat()
    turno_filtro = request.GET.get('turno', '')
    turma_filtro = request.GET.get('turma', '')
    aluno_filtro = request.GET.get('aluno', '').strip()
    pedagoga_filtro = request.GET.get('pedagoga', '')
    tipo_filtro = request.GET.get('tipo', '')

    try:
        data_consulta = datetime.strptime(data_filtro, '%Y-%m-%d').date()
    except ValueError:
        data_consulta = timezone.now().date()
        data_filtro = data_consulta.isoformat()

    ocorrencias = RegistroOcorrenciaAluno.objects.filter(data=data_consulta).exclude(
        tipo_ocorrencia__in=TIPOS_OCORRENCIA_LEGADOS_FALTA
    ).select_related('aluno', 'aluno__turma')

    if turno_filtro:
        ocorrencias = ocorrencias.filter(Q(turno=turno_filtro) | Q(aluno__turma__turno=turno_filtro))
    if turma_filtro:
        ocorrencias = ocorrencias.filter(aluno__turma_id=turma_filtro)
    if aluno_filtro:
        ocorrencias = ocorrencias.filter(aluno__nome__icontains=aluno_filtro)
    if pedagoga_filtro:
        ocorrencias = ocorrencias.filter(atendido_por=pedagoga_filtro)
    if tipo_filtro:
        ocorrencias = ocorrencias.filter(tipo_ocorrencia=tipo_filtro)

    ocorrencias = ocorrencias.order_by(
        'aluno__turma__turno',
        'aluno__turma__nome',
        'aluno__numero',
        '-horario_chegada',
        'aluno__nome',
    )
    turmas = Turma.objects.filter(ativa=True).order_by('turno', 'nome')
    pedagogas = (
        RegistroOcorrenciaAluno.objects.exclude(
            tipo_ocorrencia__in=TIPOS_OCORRENCIA_LEGADOS_FALTA
        ).exclude(atendido_por='')
        .values_list('atendido_por', flat=True)
        .distinct()
        .order_by('atendido_por')
    )

    return {
        'ocorrencias': ocorrencias,
        'turmas': turmas,
        'pedagogas': pedagogas,
        'tipos_ocorrencia': TIPOS_OCORRENCIA_CHOICES_PERMITIDOS,
        'data_filtro': data_filtro,
        'turno_filtro': turno_filtro,
        'turma_filtro': turma_filtro,
        'aluno_filtro': aluno_filtro,
        'pedagoga_filtro': pedagoga_filtro,
        'tipo_filtro': tipo_filtro,
    }


@ocorrencias_required
def conferencia_ocorrencias(request):
    contexto = _contexto_conferencia_ocorrencias(request)
    return render(request, 'ocorrencias/conferencia_ocorrencias.html', contexto)


@ocorrencias_required
def imprimir_conferencia_ocorrencias(request):
    contexto = _contexto_conferencia_ocorrencias(request)
    contexto['impresso_em'] = timezone.now()
    return render(request, 'ocorrencias/conferencia_ocorrencias_print.html', contexto)


@justificar_faltas_required
def justificar_faltas_alunos(request):
    if request.method == 'POST':
        falta = get_object_or_404(RegistroFaltaAluno, id=request.POST.get('falta_id'))
        justificativa = request.POST.get('justificativa', '').strip()
        responsavel = request.POST.get('responsavel_justificativa', '').strip()
        data_justificativa = request.POST.get('data_justificativa')

        if not justificativa or not responsavel or not data_justificativa:
            messages.error(request, 'Informe justificativa, responsÃ¡vel e data da justificativa.')
            return redirect('justificar_faltas_alunos')

        falta.justificada = True
        falta.justificativa = justificativa
        falta.responsavel_justificativa = responsavel
        falta.data_justificativa = data_justificativa
        falta.save(update_fields=['justificada', 'justificativa', 'responsavel_justificativa', 'data_justificativa'])
        messages.success(request, f'Justificativa registrada para {falta.aluno.nome}.')
        return redirect('justificar_faltas_alunos')

    data_filtro = request.GET.get('data', '')
    turno_filtro = request.GET.get('turno', '')
    turma_filtro = request.GET.get('turma', '')
    aluno_filtro = request.GET.get('aluno', '').strip()
    pedagoga_filtro = request.GET.get('pedagoga', '')

    faltas = RegistroFaltaAluno.objects.filter(justificada=False).select_related('aluno', 'aluno__turma')

    if data_filtro:
        faltas = faltas.filter(data=data_filtro)
    if turno_filtro:
        faltas = faltas.filter(aluno__turma__turno=turno_filtro)
    if turma_filtro:
        faltas = faltas.filter(aluno__turma_id=turma_filtro)
    if aluno_filtro:
        faltas = faltas.filter(aluno__nome__icontains=aluno_filtro)
    if pedagoga_filtro:
        faltas = faltas.filter(pedagoga=pedagoga_filtro)

    faltas = faltas.order_by('-data', 'aluno__turma__turno', 'aluno__turma__nome', 'aluno__numero')
    turmas = Turma.objects.filter(ativa=True).order_by('turno', 'nome')
    pedagogas = (
        RegistroFaltaAluno.objects.exclude(pedagoga='')
        .values_list('pedagoga', flat=True)
        .distinct()
        .order_by('pedagoga')
    )

    contexto = {
        'faltas': faltas,
        'turmas': turmas,
        'pedagogas': pedagogas,
        'data_filtro': data_filtro,
        'turno_filtro': turno_filtro,
        'turma_filtro': turma_filtro,
        'aluno_filtro': aluno_filtro,
        'pedagoga_filtro': pedagoga_filtro,
    }
    return render(request, 'faltas/justificar_faltas_alunos.html', contexto)


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def editar_falta_aluno(request, falta_id):
    falta = get_object_or_404(RegistroFaltaAluno, id=falta_id)

    if request.method == 'POST':
        falta.quantidade_faltas = request.POST.get('quantidade', 1)
        falta.justificada = request.POST.get('justificada') == 'on'
        falta.responsavel_contatado = request.POST.get('responsavel', '')
        falta.observacoes = request.POST.get('observacoes', '')
        falta.save()

        messages.success(request, 'Registro atualizado!')
        return redirect('controle_faltas_alunos')

    return render(request, 'faltas/editar_falta_aluno.html', {'falta': falta})

@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def excluir_falta_aluno(request, falta_id):
    falta = get_object_or_404(RegistroFaltaAluno, id=falta_id)
    falta.delete()
    messages.success(request, 'Registro excluido!')
    return redirect('controle_faltas_alunos')

@ocorrencias_required
def registrar_ocorrencia_aluno(request):
    tipos_ocorrencia_permitidos = TIPOS_OCORRENCIA_PERMITIDOS
    ultimas_ocorrencias = RegistroOcorrenciaAluno.objects.select_related('aluno', 'aluno__turma').order_by('-data', '-horario_chegada')[:10]

    if request.method == 'POST':
        # Salva a turma e turno na memória
        turma_post_id = request.POST.get('turma')
        if turma_post_id:
            request.session['ultima_turma_ocorrencia_id'] = turma_post_id

        pedagoga = request.POST.get('pedagoga', '').strip()
        form = RegistroOcorrenciaForm(request.POST)

        if form.is_valid():
            turma = form.cleaned_data['turma']
            numero = form.cleaned_data['numero_aluno']
            data_form = form.cleaned_data['data']

            aluno = Aluno.objects.filter(turma=turma, numero=numero).first()
            if not aluno:
                messages.error(request, f'Aluno numero {numero} nao encontrado na turma {turma.nome}!')
                return render(request, 'ocorrencias/registrar_ocorrencia.html', {'form': form, 'ultimas_ocorrencias': ultimas_ocorrencias})

            logger.info('Turma normalizada para mapeamento de pedagoga: %s', normalizar_turma_mapeamento(aluno.turma))
            pedagoga = resolver_pedagoga_turma(aluno.turma, pedagoga)
            ocorrencia = form.save(commit=False)
            tipo_normalizado = normalizar_tipo_ocorrencia(request.POST.get('tipo_ocorrencia', 'atraso'))

            if tipo_normalizado not in tipos_ocorrencia_permitidos:
                messages.error(request, 'Tipo de ocorrencia invalido. Faltas devem ser registradas em Faltas de Alunos.')
                return render(request, 'ocorrencias/registrar_ocorrencia.html', {'form': form, 'ultimas_ocorrencias': ultimas_ocorrencias})

            # ===== TRAVA 3: Bloqueia atraso se já houver falta no mesmo dia =====
            if tipo_normalizado == 'atraso':
                if RegistroFaltaAluno.objects.filter(aluno=aluno, data=data_form).exists():
                    messages.error(request, f'⛔ BLOQUEADO: {aluno.nome} já possui FALTA registrada hoje. Não é possível registrar Atraso.')
                    return render(request, 'ocorrencias/registrar_ocorrencia.html', {'form': form, 'ultimas_ocorrencias': ultimas_ocorrencias})

            # ===== TRAVA 4: Bloqueia ocorrência duplicada (mesmo tipo no mesmo dia) =====
            if RegistroOcorrenciaAluno.objects.filter(aluno=aluno, data=data_form, tipo_ocorrencia=tipo_normalizado).exists():
                messages.warning(request, f'Atenção: Já existe uma ocorrência do tipo "{tipo_normalizado}" registrada para {aluno.nome} nesta data.')
                return render(request, 'ocorrencias/registrar_ocorrencia.html', {'form': form, 'ultimas_ocorrencias': ultimas_ocorrencias})

            ocorrencia.tipo_ocorrencia = tipo_normalizado
            ocorrencia.observacoes_adicionais = request.POST.get('observacoes_adicionais', '')
            ocorrencia.atendido_por = pedagoga
            ocorrencia.turno = request.POST.get('turno', 'integral')
            ocorrencia.faltou = False

            if ocorrencia.horario_chegada == '':
                ocorrencia.horario_chegada = None
            if ocorrencia.horario_contato == '':
                ocorrencia.horario_contato = None

            try:
                ocorrencia.aluno = aluno
                ocorrencia.registrado_por = request.user
                with transaction.atomic():
                    ocorrencia.save()
            except IntegrityError:
                messages.error(request, 'Erro: Registro duplicado no banco de dados.')
                return render(request, 'ocorrencias/registrar_ocorrencia.html', {'form': form, 'ultimas_ocorrencias': ultimas_ocorrencias})

            request.session['ultima_turma_ocorrencia_nome'] = turma.nome
            request.session['ultimo_tipo_ocorrencia'] = tipo_normalizado

            messages.success(request, f'{MENSAGEM_OCORRENCIA_DIARIA} Aluno: {aluno.nome} - Turma: {turma.nome}, No {numero}.')
            return redirect('registrar_ocorrencia_aluno')
        else:
            messages.error(request, 'Erro no formulario. Verifique os dados.')
            return render(request, 'ocorrencias/registrar_ocorrencia.html', {'form': form, 'ultimas_ocorrencias': ultimas_ocorrencias})

    else:
        # ===== A MÁGICA PARA DESTRAVAR A DATA ACONTECE AQUI =====
        # Apaga a data antiga que estava salva no navegador
        if 'ultima_data_ocorrencia' in request.session:
            del request.session['ultima_data_ocorrencia']

        ultima_turma_id = request.session.get('ultima_turma_ocorrencia_id')
        ultima_turma = Turma.objects.filter(id=ultima_turma_id).first() if ultima_turma_id else None

        ultimo_tipo = request.session.get('ultimo_tipo_ocorrencia', 'atraso')
        if ultimo_tipo not in tipos_ocorrencia_permitidos:
            ultimo_tipo = 'atraso'

        initial_data = {
            'data': timezone.now().date().isoformat(), # <--- FORÇA A DATA DE HOJE
            'faltou': False,
            'tipo_ocorrencia': ultimo_tipo,
            'turno': request.session.get('ultimo_turno', 'integral'),
        }
        if ultima_turma:
            initial_data['turma'] = ultima_turma

        form = RegistroOcorrenciaForm(initial=initial_data)

    contexto = {
        'form': form,
        'ultimas_ocorrencias': ultimas_ocorrencias
    }
    return render(request, 'ocorrencias/registrar_ocorrencia.html', contexto)

def buscar_aluno_ajax(request):
    turma_id = request.GET.get('turma_id')
    numero = request.GET.get('numero', '')
    data = request.GET.get('data', '')
    tipo_ocorrencia = request.GET.get('tipo_ocorrencia', '')

    if turma_id and numero:
        try:
            turma = Turma.objects.get(id=turma_id)
            aluno = Aluno.objects.get(turma=turma, numero=numero)
            reincidencia = False
            if data and tipo_ocorrencia in TIPOS_OCORRENCIA_REINCIDENCIA:
                reincidencia = RegistroOcorrenciaAluno.objects.filter(
                    aluno=aluno,
                    data=data,
                    tipo_ocorrencia=tipo_ocorrencia,
                ).exists()
            return JsonResponse({
                'encontrado': True,
                'nome': aluno.nome,
                'turma': turma.nome,
                'numero': aluno.numero,
                'pedagoga': '',
                'pedagoga_nao_definida': '',
                'reincidencia': reincidencia,
            })
        except (Turma.DoesNotExist, Aluno.DoesNotExist):
            return JsonResponse({
                'encontrado': False,
                'erro': 'Aluno nao encontrado nesta turma.'
            })
    return JsonResponse({'encontrado': False, 'erro': 'Informe turma e numero.'})

# =============================================================================
# BUSCA ATIVA - GESTÃƒÆ’O DE FALTAS PENDENTES
# =============================================================================

# =============================================================================
# BUSCA ATIVA - GESTÃO DE FALTAS DE ALUNOS (RegistroFaltaAluno)
# =============================================================================

@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def busca_ativa(request):
    hoje = timezone.localdate()
    mes = int(request.GET.get('mes', hoje.month))
    ano = int(request.GET.get('ano', hoje.year))
    turma_id = request.GET.get('turma')

    faltas = RegistroFaltaAluno.objects.filter(
        data__year=ano,
        data__month=mes
    ).select_related('aluno', 'aluno__turma').order_by('-data', 'aluno__turma__nome', 'aluno__numero')

    # Filtra por turma se selecionada
    if turma_id:
        faltas = faltas.filter(aluno__turma_id=turma_id)

    # Lista de turmas para o dropdown
    turmas = Turma.objects.filter(ativa=True).order_by('nome_completo')

    # Resumo por turma
    turmas_resumo = {}
    for falta in faltas:
        turma_nome = falta.aluno.turma.nome
        if turma_nome not in turmas_resumo:
            turmas_resumo[turma_nome] = {'total': 0, 'realizadas': 0}
        turmas_resumo[turma_nome]['total'] += 1
        if falta.busca_ativa_realizada:
            turmas_resumo[turma_nome]['realizadas'] += 1

    context = {
        'ocorrencias': faltas,
        'turmas': turmas,
        'turma_selecionada': turma_id,
        'turmas_resumo': turmas_resumo,
        'mes': mes,
        'ano': ano,
        'meses': range(1, 13),
        'anos': range(2024, 2028),
    }
    return render(request, 'ocorrencias/busca_ativa.html', context)


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
@require_POST
def marcar_busca_ativa(request, pk):
    """Marca uma falta como 'busca ativa realizada' via POST"""
    falta = get_object_or_404(RegistroFaltaAluno, id=pk)
    falta.busca_ativa_realizada = True
    falta.save(update_fields=['busca_ativa_realizada'])
    messages.success(request, f'Busca ativa marcada para {falta.aluno.nome}')

    # Pegar os parâmetros para manter os filtros (aceita POST ou query string)
    mes = request.POST.get('mes') or request.GET.get('mes', '')
    ano = request.POST.get('ano') or request.GET.get('ano', '')
    turma_id = request.POST.get('turma') or request.GET.get('turma', '')

    # Montar a URL de volta com os mesmos filtros
    url = '/busca-ativa/'
    params = []
    if mes:
        params.append(f'mes={mes}')
    if ano:
        params.append(f'ano={ano}')
    if turma_id and turma_id != 'None':
        params.append(f'turma={turma_id}')

    if params:
        url += '?' + '&'.join(params)

    return redirect(url)


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def marcar_todos_busca_ativa(request):
    if request.method == 'POST':
        mes = request.POST.get('mes')
        ano = request.POST.get('ano')
        turma_id = request.POST.get('turma')

        faltas = RegistroFaltaAluno.objects.filter(
            data__year=ano,
            data__month=mes
        )
        if turma_id and turma_id != 'None':
            faltas = faltas.filter(aluno__turma_id=turma_id)

        quantidade = faltas.update(busca_ativa_realizada=True)
        messages.success(request, f'{quantidade} falta(s) marcada(s) como busca ativa realizada!')

    return redirect('busca_ativa')


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def editar_busca_ativa(request, pk):
    """Edita dados de contato da Busca Ativa em RegistroFaltaAluno sem alterar aluno, turma ou data."""
    from datetime import time
    falta = get_object_or_404(RegistroFaltaAluno.objects.select_related('aluno', 'aluno__turma'), id=pk)

    mes = request.GET.get('mes', '')
    ano = request.GET.get('ano', '')
    turma_id = request.GET.get('turma', '')

    if request.method == 'POST':
        responsavel = request.POST.get('responsavel_contatado', '').strip()
        alegado = request.POST.get('alegado_responsavel', '').strip()
        horario_raw = request.POST.get('horario_contato', '').strip()

        falta.responsavel_contatado = responsavel
        falta.alegado_responsavel = alegado
        falta.busca_ativa_realizada = True

        if horario_raw:
            try:
                if ':' in horario_raw:
                    partes = horario_raw.split(':')
                    hora = int(partes[0])
                    minuto = int(partes[1])
                    falta.horario_contato = time(hour=hora, minute=minuto)
                else:
                    falta.horario_contato = None
            except Exception:
                falta.horario_contato = None
        else:
            falta.horario_contato = None

        falta.save(update_fields=['responsavel_contatado', 'alegado_responsavel', 'horario_contato', 'busca_ativa_realizada'])
        messages.success(request, f'Busca Ativa atualizada para {falta.aluno.nome}!')

        url = '/busca-ativa/'
        params = []
        if mes:
            params.append(f'mes={mes}')
        if ano:
            params.append(f'ano={ano}')
        if turma_id:
            params.append(f'turma={turma_id}')

        if params:
            url += '?' + '&'.join(params)

        return redirect(url)

    context = {
        'falta': falta,
        'mes': mes,
        'ano': ano,
        'turma_selecionada': turma_id,
    }
    return render(request, 'ocorrencias/editar_busca_ativa.html', context)
