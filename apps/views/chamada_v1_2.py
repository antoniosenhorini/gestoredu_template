"""Chamada v1.2 - versao experimental de registro de faltas em grade por turma.

Nao substitui o fluxo atual (registrar_falta_aluno). E aditiva: reaproveita o
mesmo model RegistroFaltaAluno e as mesmas regras de atestado/aluno ativo,
apenas com uma tela de lancamento em lote por turma+data.

Limitacao conhecida (documentada, nao e um bug): como so gravamos registros
para quem falta (falta.md, secao 3), uma chamada em que ninguem faltou nao
deixa rastro no banco. Nesse caso a deteccao de "chamada ja lancada" nao
dispara ao reabrir a mesma turma+data - reenviar e inofensivo (nenhum dado e
duplicado, porque zero faltas seriam criadas nas duas vezes), mas o aviso
amigavel de duplicidade nao aparece. Registrar uma migration so para guardar
esse marcador nao foi feito nesta primeira etapa (instrucao explicita de
evitar migrations salvo indispensavel).

Concorrencia: RegistroFaltaAluno nao tem UniqueConstraint em (aluno, data)
(nao criamos migration para isso), entao get_or_create() sozinho nao e
atomico de verdade no nivel do banco - sem constraint, duas transacoes
concorrentes podem passar pelo get() antes de qualquer create(). Por isso,
antes de checar/gravar, bloqueamos a linha da Turma com select_for_update()
dentro do transaction.atomic() para serializar requisicoes concorrentes para
a mesma turma+data, e so entao repetimos a checagem de duplicidade e usamos
get_or_create(). Nota sobre o banco deste projeto: e SQLite (colegio/settings.py),
cujo backend Django nao emite a clausula FOR UPDATE (has_select_for_update=False) -
o select_for_update() aqui nao trava a linha via SQL, mas o proprio SQLite
serializa escritores no nivel do arquivo/conexao quando a transacao atinge a
primeira escrita, o que somado ao lock logico e a nova checagem dentro do
atomic() reduz bastante a janela de corrida. Nao e uma garantia matematica
equivalente a uma UniqueConstraint real - se isso for indispensavel, a
correcao definitiva e uma migration com UniqueConstraint(aluno, data).

Atestado direto na grade: chamada_v1_2_atestado reaproveita o model
AtestadoAluno e as mesmas regras de bloqueio de falta ja usadas em
registrar/conferir. Nao substitui o CRUD completo de atestados
(atestados_alunos, em apps/views/alunos.py) - so cobre o caso rapido de
marcar atestado durante a chamada. Se ja existe atestado ativo sobreposto ao
periodo pedido, a view reaproveita o existente em vez de criar um novo.
"""
from datetime import datetime

from django.db import transaction
from django.urls import reverse

from .utilitarios import *
from ..models import AtestadoAluno


def _parse_data(valor):
    try:
        return datetime.strptime(valor, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def _chamada_ja_registrada(turma, data):
    return RegistroFaltaAluno.objects.filter(aluno__turma=turma, data=data).exists()


def _atestados_ativos_por_aluno(alunos, data):
    """Mapa {aluno_id: AtestadoAluno} com o atestado ativo que cobre `data`."""
    atestados = AtestadoAluno.objects.filter(
        aluno__in=alunos,
        ativo=True,
        data_inicio__lte=data,
        data_fim__gte=data,
    )
    return {atestado.aluno_id: atestado for atestado in atestados}


def _atestado_sobreposto_ativo(aluno, data_inicio, data_fim):
    """Atestado ativo do aluno cujo periodo se sobrepoe ao [data_inicio, data_fim] informado."""
    return AtestadoAluno.objects.filter(
        aluno=aluno,
        ativo=True,
        data_inicio__lte=data_fim,
        data_fim__gte=data_inicio,
    ).order_by('data_inicio').first()


def _anotar_alunos_com_atestado(alunos, atestados_por_aluno):
    for aluno in alunos:
        aluno.atestado_ativo = atestados_por_aluno.get(aluno.id)
    return alunos


@faltas_required
def chamada_v1_2_selecionar(request):
    turmas_ativas = Turma.objects.filter(ativa=True).order_by('nome')
    hoje = timezone.now().date().isoformat()
    return render(request, 'chamada_v1_2/selecionar.html', {
        # Joga TODAS as turmas na lista da manhã para o sistema listar
        'turmas_manha': turmas_ativas, 
        # Deixa a da tarde vazia para não duplicar na tela
        'turmas_tarde': Turma.objects.none(), 
        'hoje': hoje,
    })


@faltas_required
def chamada_v1_2_registrar(request):
    if request.method == 'POST':
        turma_id = request.POST.get('turma')
        data = _parse_data(request.POST.get('data'))
        falta_ids = set(request.POST.getlist('falta_ids'))

        turma = Turma.objects.filter(id=turma_id, ativa=True).first()
        if not turma or not data:
            return JsonResponse({'status': 'erro', 'mensagem': 'Turma ou data invalida.'}, status=400)

        with transaction.atomic():
            # Bloqueia a linha da turma para serializar requisicoes concorrentes
            # para a mesma turma+data. Sem UniqueConstraint em RegistroFaltaAluno
            # (nao criamos migration para isso), esse lock + a nova checagem de
            # duplicidade logo abaixo sao a unica barreira contra corrida entre
            # duas submissoes simultaneas da mesma chamada.
            turma_bloqueada = Turma.objects.select_for_update().get(id=turma.id)

            if _chamada_ja_registrada(turma_bloqueada, data):
                return JsonResponse({
                    'status': 'duplicado',
                    'mensagem': 'Ja existe uma chamada cadastrada para esta turma nesta data. '
                                'Voce pode abrir a tela de conferencia para editar.',
                }, status=409)

            alunos = list(Aluno.objects.filter(turma=turma_bloqueada, ativo=True))
            alunos_por_id = {str(aluno.id): aluno for aluno in alunos}
            atestado_ids = set(_atestados_ativos_por_aluno(alunos, data).keys())

            registros_existentes = set(
                RegistroFaltaAluno.objects.filter(aluno__in=alunos, data=data).values_list('aluno_id', flat=True)
            )

            criadas = 0
            for aluno_id in falta_ids:
                aluno = alunos_por_id.get(aluno_id)
                if not aluno or aluno.id in atestado_ids or aluno.id in registros_existentes:
                    continue
                _, criada = RegistroFaltaAluno.objects.get_or_create(
                    aluno=aluno,
                    data=data,
                    defaults={
                        'quantidade_faltas': 1,
                        'registrado_por': request.user,
                    },
                )
                if criada:
                    criadas += 1

        return JsonResponse({
            'status': 'ok',
            'mensagem': 'Chamada registrada com sucesso.',
            'total_alunos': len(alunos),
            'presentes': len(alunos) - criadas,
            'faltosos': criadas,
        })

    turma_id = request.GET.get('turma')
    data = _parse_data(request.GET.get('data')) or timezone.now().date()

    turma = Turma.objects.filter(id=turma_id, ativa=True).first()
    if not turma:
        messages.error(request, 'Selecione uma turma valida para iniciar a chamada.')
        return redirect('chamada_v1_2_selecionar')

    if _chamada_ja_registrada(turma, data):
        messages.warning(
            request,
            'Ja existe uma chamada cadastrada para esta turma nesta data. '
            'Voce pode abrir a tela de conferencia para editar.',
        )
        return redirect(f"{reverse('chamada_v1_2_conferir')}?turma={turma.id}&data={data.isoformat()}")

    alunos = list(Aluno.objects.filter(turma=turma, ativo=True).order_by('numero'))
    atestados_por_aluno = _atestados_ativos_por_aluno(alunos, data)
    atestado_ids = set(atestados_por_aluno.keys())
    _anotar_alunos_com_atestado(alunos, atestados_por_aluno)

    # Força o turno a aparecer como Integral se estiver vazio no banco
    turno_exibicao = turma.turno if turma.turno else 'Integral'

    return render(request, 'chamada_v1_2/registrar.html', {
        'turma': turma,
        'data': data,
        'alunos': alunos,
        'atestado_ids': atestado_ids,
        'turno_label': turno_exibicao, # <--- Enviamos a variável nova
        'alunos_json': [
            {'id': str(aluno.id), 'numero': aluno.numero, 'nome': aluno.nome}
            for aluno in alunos
        ],
    })


@justificar_faltas_required
def chamada_v1_2_atestado(request):
    """Cadastra (ou reaproveita) um atestado ativo direto da grade da Chamada v1.2.

    Restrito a quem ja pode justificar faltas (superuser/Equipe Diretiva) -
    mesma permissao do CRUD completo de atestados (atestados_alunos), que
    continua com seu proprio fluxo, intocado. Digitadores continuam
    registrando presente/falta normalmente na grade, mas nao veem o botao
    ATESTADO nem podem chamar este endpoint diretamente (403). Esta view
    cobre apenas o caso de uso rapido "aluno tem atestado, marcar aqui" e
    delega qualquer edicao/exclusao mais elaborada ao fluxo ja existente.

    Se ja existir um atestado ativo do aluno que se sobrepoe ao periodo
    informado, nao cria um novo (evita duplicidade) e devolve o periodo ja
    cadastrado. So cria um AtestadoAluno novo quando nao ha sobreposicao.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'erro', 'mensagem': 'Metodo nao permitido.'}, status=405)

    turma_id = request.POST.get('turma')
    aluno_id = request.POST.get('aluno')
    data_atual = _parse_data(request.POST.get('data'))
    data_inicio = _parse_data(request.POST.get('data_inicio'))
    data_fim = _parse_data(request.POST.get('data_fim'))

    turma = Turma.objects.filter(id=turma_id).first()
    if not turma or not data_atual:
        return JsonResponse({'status': 'erro', 'mensagem': 'Turma ou data da chamada invalida.'}, status=400)

    if not data_inicio or not data_fim:
        return JsonResponse({'status': 'erro', 'mensagem': 'Informe data inicial e data final do atestado.'}, status=400)

    if data_fim < data_inicio:
        return JsonResponse({'status': 'erro', 'mensagem': 'Data final nao pode ser anterior a data inicial.'}, status=400)

    with transaction.atomic():
        # Bloqueia a linha do aluno para serializar tentativas concorrentes
        # de cadastrar atestado para o mesmo aluno (mesmo raciocinio do lock
        # de Turma usado em chamada_v1_2_registrar/conferir_salvar).
        aluno = Aluno.objects.select_for_update().filter(id=aluno_id, turma_id=turma.id, ativo=True).first()
        if not aluno:
            return JsonResponse({
                'status': 'erro',
                'mensagem': 'Aluno nao encontrado ou nao pertence a turma informada.',
            }, status=400)

        atestado_existente = _atestado_sobreposto_ativo(aluno, data_inicio, data_fim)
        if atestado_existente:
            atestado = atestado_existente
            criado = False
        else:
            atestado = AtestadoAluno.objects.create(
                aluno=aluno,
                data_inicio=data_inicio,
                data_fim=data_fim,
                ativo=True,
                observacao='Cadastrado pela Chamada v1.2.',
                criado_por=request.user,
            )
            criado = True

        # Consistencia chamada <-> atestado: se o periodo cobre a data desta
        # chamada, o aluno nao pode continuar com falta ja lancada nela.
        if atestado.data_inicio <= data_atual <= atestado.data_fim:
            RegistroFaltaAluno.objects.filter(aluno=aluno, data=data_atual).delete()

    return JsonResponse({
        'status': 'ok',
        'criado': criado,
        'mensagem': 'Atestado cadastrado com sucesso.' if criado else 'Ja existe atestado ativo cobrindo esse periodo.',
        'atestado': {
            'data_inicio': atestado.data_inicio.isoformat(),
            'data_fim': atestado.data_fim.isoformat(),
        },
    })


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def chamada_v1_2_conferir(request):
    turma_id = request.GET.get('turma')
    data = _parse_data(request.GET.get('data'))
    turmas = Turma.objects.filter(ativa=True).order_by('turno', 'nome')

    contexto = {
        'turmas': turmas,
        'turma_selecionada': None,
        'data_selecionada': data,
        'alunos': [],
        'turma_id_invalido': False,
    }

    if not turma_id or not data:
        return render(request, 'chamada_v1_2/conferir.html', contexto)

    turma = Turma.objects.filter(id=turma_id).first()
    if not turma:
        messages.error(request, 'Turma nao encontrada.')
        contexto['turma_id_invalido'] = True
        return render(request, 'chamada_v1_2/conferir.html', contexto)

    alunos = list(Aluno.objects.filter(turma=turma, ativo=True).order_by('numero'))
    atestados_por_aluno = _atestados_ativos_por_aluno(alunos, data)
    atestado_ids = set(atestados_por_aluno.keys())
    _anotar_alunos_com_atestado(alunos, atestados_por_aluno)
    faltas_existentes = set(
        RegistroFaltaAluno.objects.filter(aluno__in=alunos, data=data).values_list('aluno_id', flat=True)
    )

    contexto.update({
        'turma_selecionada': turma,
        'data_selecionada': data,
        'alunos': alunos,
        'atestado_ids': atestado_ids,
        'faltas_existentes': faltas_existentes,
    })
    return render(request, 'chamada_v1_2/conferir.html', contexto)


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def chamada_v1_2_conferir_salvar(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'erro', 'mensagem': 'Metodo nao permitido.'}, status=405)

    turma_id = request.POST.get('turma')
    data = _parse_data(request.POST.get('data'))
    falta_ids_desejadas = set(request.POST.getlist('falta_ids'))

    turma = Turma.objects.filter(id=turma_id).first()
    if not turma or not data:
        return JsonResponse({'status': 'erro', 'mensagem': 'Turma ou data invalida.'}, status=400)

    with transaction.atomic():
        # Mesmo lock usado em chamada_v1_2_registrar: serializa edicoes
        # concorrentes da mesma turma+data antes de ler/gravar os registros.
        turma_bloqueada = Turma.objects.select_for_update().get(id=turma.id)

        alunos = list(Aluno.objects.filter(turma=turma_bloqueada, ativo=True))
        alunos_por_id = {str(aluno.id): aluno for aluno in alunos}
        atestado_ids = set(_atestados_ativos_por_aluno(alunos, data).keys())

        registros_atuais = {
            str(registro.aluno_id): registro
            for registro in RegistroFaltaAluno.objects.filter(aluno__in=alunos, data=data)
        }

        adicionadas = 0
        removidas = 0

        for aluno_id, aluno in alunos_por_id.items():
            deveria_ter_falta = aluno_id in falta_ids_desejadas and aluno.id not in atestado_ids
            tem_falta = aluno_id in registros_atuais

            if deveria_ter_falta and not tem_falta:
                _, criada = RegistroFaltaAluno.objects.get_or_create(
                    aluno=aluno,
                    data=data,
                    defaults={
                        'quantidade_faltas': 1,
                        'registrado_por': request.user,
                    },
                )
                if criada:
                    adicionadas += 1
            elif not deveria_ter_falta and tem_falta:
                registros_atuais[aluno_id].delete()
                removidas += 1

    return JsonResponse({
        'status': 'ok',
        'mensagem': 'Chamada atualizada com sucesso.',
        'adicionadas': adicionadas,
        'removidas': removidas,
    })
