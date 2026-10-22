from .utilitarios import *

# =============================================================================
# GESTAO DE LABORATORIOS E EMPRESTIMOS
# =============================================================================

from ..models import (
    Laboratorio, ItemEquipamento, AgendamentoLab, Emprestimo, Professor, Turma,
    ConfiguracaoCronogramaLaboratorio, SemanaCronogramaLaboratorio,
)
from django.db.models import Count, Max, Q
from django.db import transaction, IntegrityError
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET
from datetime import date, datetime, timedelta
import hashlib


DIAS_SEMANA_LAB = ['Segunda-feira', 'Terca-feira', 'Quarta-feira', 'Quinta-feira', 'Sexta-feira']
HORARIOS_LAB = ['1', '2', '3', '4', '5', '6']
TURNOS_LAB = [
    ('manha', 'Manha'),
    ('tarde', 'Tarde'),
]
MESES_PT_BR = [
    '',
    'Janeiro',
    'Fevereiro',
    'Março',
    'Abril',
    'Maio',
    'Junho',
    'Julho',
    'Agosto',
    'Setembro',
    'Outubro',
    'Novembro',
    'Dezembro',
]
MAPA_DIAS_LAB = {
    'Monday': 'Segunda-feira',
    'Tuesday': 'Terca-feira',
    'Wednesday': 'Quarta-feira',
    'Thursday': 'Quinta-feira',
    'Friday': 'Sexta-feira',
    'Saturday': 'Sabado',
    'Sunday': 'Domingo',
}
LABS_IGNORADOS_COMPLETUDE = {'4', '04', '5', '05'}


def _laboratorios_agendaveis():
    return Laboratorio.objects.filter(ativo=True).order_by('nome')


def _laboratorio_considerado_cronograma(laboratorio):
    nome = ''.join(caractere for caractere in laboratorio.nome if caractere.isdigit())
    return nome not in LABS_IGNORADOS_COMPLETUDE


def _agendamentos_labs_considerados():
    ids_labs_considerados = [
        laboratorio.id
        for laboratorio in _laboratorios_agendaveis()
        if _laboratorio_considerado_cronograma(laboratorio)
    ]
    return AgendamentoLab.objects.filter(laboratorio_id__in=ids_labs_considerados)


def _laboratorios_filtrados(laboratorio_id):
    laboratorios = _laboratorios_agendaveis()
    if laboratorio_id:
        laboratorios = laboratorios.filter(id=laboratorio_id)
    return laboratorios


def _slots_cronograma(turno_filtro):
    turnos = [turno for turno in TURNOS_LAB if turno[0] == turno_filtro] if turno_filtro in ['manha', 'tarde'] else TURNOS_LAB
    return [
        {
            'turno': turno,
            'turno_label': turno_label,
            'horario': horario,
            'horario_label': f'Aula{horario}',
        }
        for turno, turno_label in turnos
        for horario in HORARIOS_LAB
    ]


def _dias_cronograma(data_inicio):
    """Colunas de dias uteis, com datas reais e ordem visual escolar.

    Fase 2 permite data_inicio em qualquer dia da semana (nao so segunda) -
    por isso o nome do dia NUNCA pode vir de uma lista fixa por posicao
    (DIAS_SEMANA_LAB), so da data real, com o mesmo mapeamento usado por
    _agendamentos_por_chave/_agendamentos_cards (MAPA_DIAS_LAB sobre
    data.strftime('%A')) - senao a celula que a grade procura nunca bate com
    a chave que o agendamento foi salvo. Varre os 7 dias da janela inteira da
    versao (data_inicio ate data_fim = data_inicio + 6, ver
    SemanaCronogramaLaboratorio.data_fim) mas so devolve segunda a
    sexta-feira - sabado/domingo sao ignorados so na APRESENTACAO da grade;
    a identidade da versao (data_inicio) e o isolamento por semana_id nao
    mudam, e um agendamento eventualmente existente num sabado/domingo
    (data legada ou criado antes deste ajuste) continua no banco, so nao
    ganha coluna nesta tela. A lista final e ordenada apenas para exibicao,
    sempre de segunda a sexta, sem normalizar data_inicio nem trocar a data
    real associada a cada coluna."""
    dias = []
    for indice in range(7):
        data_dia = data_inicio + timedelta(days=indice)
        if data_dia.weekday() >= 5:  # 5=sabado, 6=domingo
            continue
        dia_en = data_dia.strftime('%A')
        dias.append({
            'nome': MAPA_DIAS_LAB.get(dia_en, dia_en),
            'data': data_dia,
        })
    return sorted(dias, key=lambda dia: dia['data'].weekday())


def _inicio_semana(data):
    return data - timedelta(days=data.weekday())


def _campo_semana_editando(turno):
    """Nome do campo em ConfiguracaoCronogramaLaboratorio que guarda a semana
    em preparacao daquele turno."""
    return f'data_inicio_{turno}_editando'


def _semana_editando(turno):
    """Semana (segunda-feira) marcada como 'em edicao' para o turno.

    Lida direto de ConfiguracaoCronogramaLaboratorio - estado compartilhado
    entre toda a equipe diretiva (antes ficava preso a sessao de cada
    usuario em request.session, entao um administrador nunca via a semana
    que outro estava preparando). Continua sem nenhuma relacao com o
    cronograma ATIVO (data_inicio_manha_ativo/data_inicio_tarde_ativo), que
    e um campo separado e so muda quando alguem ativa explicitamente.
    """
    configuracao = ConfiguracaoCronogramaLaboratorio.objects.first()
    if not configuracao:
        return None
    return getattr(configuracao, _campo_semana_editando(turno))


def _mes_ano_cronograma(data):
    return f'{MESES_PT_BR[data.month]}/{data.year}'


# =============================================================================
# FASE 2 - HELPERS DE SemanaCronogramaLaboratorio
# =============================================================================
#
# Estrategia de transicao (ver secao 8 do relatorio da Fase 2): por turno,
# enquanto NENHUMA linha de SemanaCronogramaLaboratorio existir para aquele
# turno ("nao adotado"), toda a leitura/escrita de estado ativo/editando
# continua 100% pelo mecanismo antigo (campos de ConfiguracaoCronogramaLaboratorio
# e o marcador unico _semana_editando), exatamente como antes desta fase -
# isso preserva instalacoes/fluxos que ainda nao passaram por nenhuma acao
# nova. No momento em que a PRIMEIRA linha e criada para um turno (via
# "Criar semana", ou pela migracao de dados da Fase 1), aquele turno passa a
# ser tratado como "adotado": a partir dai, SemanaCronogramaLaboratorio e a
# UNICA fonte consultada para decidir o que e ativa/editando/historica/
# cancelada e o que pode receber escrita naquele turno - os campos antigos
# deixam de ser lidos (so continuam sendo escritos, como espelho/compatibilidade,
# nas acoes de ativar/desativar). Isso evita duas fontes de verdade
# divergentes coexistindo para o mesmo turno.
def _turno_usa_semana_nova(turno):
    """True quando o turno ja tem pelo menos 1 linha em
    SemanaCronogramaLaboratorio (ja "adotou" a nova estrutura)."""
    if turno not in ('manha', 'tarde'):
        return False
    return SemanaCronogramaLaboratorio.objects.filter(turno=turno).exists()


def _semana_ativa_obj(turno):
    """SemanaCronogramaLaboratorio com status='ativa' do turno, se existir."""
    if turno not in ('manha', 'tarde'):
        return None
    return SemanaCronogramaLaboratorio.objects.filter(turno=turno, status='ativa').first()


def _semanas_editando_qs(turno):
    """Todas as semanas em edicao do turno, da mais antiga para a mais nova."""
    return SemanaCronogramaLaboratorio.objects.filter(turno=turno, status='editando').order_by('data_inicio')


def _semanas_historicas_qs(turno):
    """Historico somente leitura do turno, da semana mais recente para a mais antiga."""
    return SemanaCronogramaLaboratorio.objects.filter(
        turno=turno, status='historica',
    ).annotate(horarios_total=Count('agendamentos')).order_by('-data_inicio')


def _semanas_canceladas_qs(turno):
    """Semanas canceladas do turno, somente leitura, da mais antiga para a
    mais nova. Usada pelo painel 'Datas removidas' do diagnostico, para o
    administrador conferir quais datas ja foram usadas e canceladas - e por
    isso continuam reservadas (ver unica_semana_nao_historica_por_turno_data)
    e nao podem ser recriadas."""
    return SemanaCronogramaLaboratorio.objects.filter(
        turno=turno, status='cancelada',
    ).select_related('cancelado_por').annotate(
        horarios_total=Count('agendamentos'),
    ).order_by('data_inicio')


def _semana_por_turno_e_data(turno, data_inicio):
    if turno not in ('manha', 'tarde') or not data_inicio:
        return None
    return SemanaCronogramaLaboratorio.objects.filter(turno=turno, data_inicio=data_inicio).first()


def _estado_semana_para_exibicao(turno, data_inicio, ativo_inicio):
    """Determina o estado ('ativa'/'editando'/'historica'/'cancelada'/None) de
    uma semana exibida na grade, para o titulo e para pode_editar_grade.

    Usa SemanaCronogramaLaboratorio quando o turno ja a adotou; senao usa a
    regra antiga (ativo_inicio calculado por obter_cronograma_ativo, e o
    marcador unico _semana_editando) - ver nota acima sobre a estrategia de
    transicao por turno.
    """
    if turno in ('manha', 'tarde') and _turno_usa_semana_nova(turno):
        semana = _semana_por_turno_e_data(turno, data_inicio)
        if semana:
            return semana.status, semana
        return None, None

    if data_inicio == ativo_inicio:
        return 'ativa', None
    if turno in ('manha', 'tarde') and data_inicio == _semana_editando(turno):
        return 'editando', None
    return None, None


def _sincronizar_campo_ativo_antigo(turno, data_inicio, data_fim):
    """Compatibilidade temporaria (Fase 2): espelha a semana ATIVA da nova
    tabela nos campos antigos data_inicio_<turno>_ativo/data_fim_<turno>_ativo,
    tocando somente o turno informado (nunca o outro) e nunca usando um
    .save() completo da configuracao compartilhada - so UPDATE dos 2 campos
    especificos daquele turno. Os campos antigos *_editando NAO sao
    sincronizados: nao conseguem representar múltiplas semanas em edicao,
    entao a partir desta fase deixam de ser escritos (continuam existindo
    apenas como remanescente congelado da Fase 1, para eventual rollback)."""
    campo_inicio = f'data_inicio_{turno}_ativo'
    campo_fim = f'data_fim_{turno}_ativo'
    linhas_afetadas = ConfiguracaoCronogramaLaboratorio.objects.update(**{
        campo_inicio: data_inicio,
        campo_fim: data_fim,
    })
    if not linhas_afetadas:
        ConfiguracaoCronogramaLaboratorio.objects.create(
            data_inicio_cronograma_ativo=date(1900, 1, 1),
            data_fim_cronograma_ativo=date(1900, 1, 7),
            **{campo_inicio: data_inicio, campo_fim: data_fim},
        )


def _limpar_campo_ativo_antigo(turno):
    """Compatibilidade temporaria (Fase 2): limpa somente os campos antigos
    _ativo do turno informado (nunca o outro), sem .save() completo."""
    campo_inicio = f'data_inicio_{turno}_ativo'
    campo_fim = f'data_fim_{turno}_ativo'
    ConfiguracaoCronogramaLaboratorio.objects.update(**{
        campo_inicio: None,
        campo_fim: None,
    })


def obter_cronograma_ativo(turno=''):
    """Semana ATIVA de um turno (ou do cronograma legado, se turno='').

    Fonte de verdade (Fase 2): SemanaCronogramaLaboratorio com status='ativa'
    do turno. Os campos antigos de ConfiguracaoCronogramaLaboratorio (e a
    adivinhacao a partir de AgendamentoLab cru, no fallback final) so sao
    consultados para um turno que AINDA NAO ADOTOU a tabela nova
    (_turno_usa_semana_nova) - assim que a primeira linha existe para um
    turno, ele passa a ser a UNICA fonte para aquele turno: se nao houver
    nenhuma 'ativa' naquele momento, o correto e responder "nao ha
    cronograma ativo" (None, None), nunca cair de volta para uma fonte
    antiga/adivinhada so porque a nova tabela nao tem uma 'ativa' agora
    (isso violaria a regra de que so status='ativa' define o cronograma
    ativo - uma semana 'editando'/'historica'/'cancelada' daquele turno
    nunca deve ser usada no lugar dela).
    """
    if turno in ('manha', 'tarde') and _turno_usa_semana_nova(turno):
        semana = _semana_ativa_obj(turno)
        if semana:
            return semana.data_inicio, semana.data_fim
        return None, None

    configuracao = ConfiguracaoCronogramaLaboratorio.objects.first()

    if configuracao:
        # Os campos "_editando" tambem contam aqui: assim que alguem prepara
        # uma semana para qualquer turno, a linha ja "conhece" a separacao
        # por turno, e nao deve mais cair no fallback do campo legado unico
        # (que preencheria "ativo" com um valor que nunca foi de fato
        # ativado). Sem isso, so preparar uma semana - sem clicar em Ativar -
        # faria os dois turnos parecerem ter um cronograma ativo.
        campos_novos_vazios = not any([
            configuracao.data_inicio_manha_ativo,
            configuracao.data_fim_manha_ativo,
            configuracao.data_inicio_tarde_ativo,
            configuracao.data_fim_tarde_ativo,
            configuracao.data_inicio_manha_editando,
            configuracao.data_inicio_tarde_editando,
        ])

        if turno == 'manha':
            if campos_novos_vazios:
                return (
                    configuracao.data_inicio_cronograma_ativo,
                    configuracao.data_fim_cronograma_ativo,
                )
            return (
                configuracao.data_inicio_manha_ativo,
                configuracao.data_fim_manha_ativo,
            )

        if turno == 'tarde':
            if campos_novos_vazios:
                return (
                    configuracao.data_inicio_cronograma_ativo,
                    configuracao.data_fim_cronograma_ativo,
                )
            return (
                configuracao.data_inicio_tarde_ativo,
                configuracao.data_fim_tarde_ativo,
            )

        return (
            configuracao.data_inicio_cronograma_ativo,
            configuracao.data_fim_cronograma_ativo,
        )

    return _semana_geral_completa_laboratorios(turno)


def _versao_ativa_turno(turno):
    """(filtro_Q, identidade) da versao ATIVA do turno, para uso em consultas
    somente-leitura voltadas ao professor (consulta publica e seu polling de
    versao). filtro_Q e identidade sao None quando nao ha versao ativa.

    filtro_Q seleciona os AgendamentoLab da versao ativa. Quando o turno ja
    adotou SemanaCronogramaLaboratorio (_turno_usa_semana_nova), filtra pela
    FK 'semana' da linha com status='ativa' - nunca por intervalo de datas.
    Isso e obrigatorio porque duas versoes do mesmo turno podem ter
    data_inicio igual ou sobreposta (o unique constraint do modelo so
    proibe isso entre 'editando'/'ativa'; uma 'historica' pode compartilhar
    data_inicio com a 'ativa' atual) - um filtro por data__range misturaria
    agendamentos de uma versao historica/cancelada com os da versao ativa.
    A FK identifica a versao exata sem ambiguidade.

    Para turnos que ainda nao adotaram a tabela nova, mantem o fallback
    legado por intervalo de datas (obter_cronograma_ativo), preservando o
    comportamento de instalacoes que nunca passaram por nenhuma acao da
    Fase 2.

    identidade e um valor opaco e hashavel que muda sempre que a versao
    ativa exibida muda - inclui o id da propria linha (nao so a
    data_inicio), para que uma troca de ativa que reaproveite a data_inicio
    de uma versao anterior (ex.: historica com a mesma data_inicio da nova
    ativa) ainda seja detectada pelo fingerprint de polling.
    """
    if turno not in ('manha', 'tarde'):
        return None, None

    if _turno_usa_semana_nova(turno):
        semana = _semana_ativa_obj(turno)
        if not semana:
            return None, None
        return Q(semana=semana), ('nova', semana.id, semana.data_inicio)

    inicio, fim = obter_cronograma_ativo(turno)
    if not inicio or not fim:
        return None, None
    return Q(turno=turno, data__range=(inicio, fim)), ('legado', inicio, fim)


def _periodo_cronograma(request, turno_forcado=None):
    data_inicio_manual = request.GET.get('data_inicio')
    if data_inicio_manual:
        # data_inicio identifica uma versao de SemanaCronogramaLaboratorio.
        # Pode ser qualquer dia: nao a convertemos para segunda-feira.
        data_inicio = datetime.strptime(data_inicio_manual, '%Y-%m-%d').date()
        return data_inicio, data_inicio + timedelta(days=6)

    turno_filtro = turno_forcado if turno_forcado else request.GET.get('turno', '')

    if turno_filtro in ['manha', 'tarde']:
        data_inicio, data_fim = obter_cronograma_ativo(turno_filtro)
        if data_inicio:
            return data_inicio, data_fim

    data_inicio = _data_inicio_cronograma(
        request,
        request.GET.get('laboratorio', ''),
        turno_filtro,
    )
    return data_inicio, data_inicio + timedelta(days=6)


def _data_inicio_cronograma(request, laboratorio_id='', turno_filtro=''):
    data_inicio = request.GET.get('data_inicio')
    if data_inicio:
        # Quando explicita, a data e a identidade exata da versao aberta.
        return datetime.strptime(data_inicio, '%Y-%m-%d').date()

    agendamentos = AgendamentoLab.objects.all()
    if laboratorio_id:
        agendamentos = agendamentos.filter(laboratorio_id=laboratorio_id)
    if turno_filtro in ['manha', 'tarde']:
        agendamentos = agendamentos.filter(turno=turno_filtro)

    data_recente = agendamentos.aggregate(data=Max('data'))['data']
    if data_recente:
        return _inicio_semana(data_recente)

    return _inicio_semana(datetime.now().date())


def _agendamentos_por_chave(agendamentos):
    agendamentos_dict = {}
    for agendamento in agendamentos:
        dia_en = agendamento.data.strftime('%A')
        dia_pt = MAPA_DIAS_LAB.get(dia_en, dia_en)
        agendamentos_dict[(agendamento.laboratorio.id, dia_pt, agendamento.turno, agendamento.horario)] = agendamento
    return agendamentos_dict


def _agendamentos_cards(agendamentos):
    cards = []
    turno_labels = dict(TURNOS_LAB)
    for agendamento in agendamentos:
        dia_en = agendamento.data.strftime('%A')
        cards.append({
            'agendamento': agendamento,
            'dia': MAPA_DIAS_LAB.get(dia_en, dia_en),
            'turno_codigo': agendamento.turno,
            'turno_label': turno_labels.get(agendamento.turno, agendamento.turno),
            'horario_label': f'Aula{agendamento.horario}',
        })
    return cards


def _grupos_turno_cronograma(turno_filtro, agendamentos_cards):
    turnos = [turno for turno in TURNOS_LAB if turno[0] == turno_filtro] if turno_filtro in ['manha', 'tarde'] else TURNOS_LAB
    estilos = {
        'manha': ('bg-primary', 'text-white'),
        'tarde': ('bg-warning', 'text-dark'),
    }
    grupos = []
    for turno, turno_label in turnos:
        bg_class, text_class = estilos[turno]
        grupos.append({
            'turno': turno,
            'turno_label': turno_label,
            'titulo': turno_label.upper(),
            'bg_class': bg_class,
            'text_class': text_class,
            'slots': _slots_cronograma(turno),
            'cards': [card for card in agendamentos_cards if card['turno_codigo'] == turno],
        })
    return grupos


def _url_cronograma_com_filtros(data_inicio='', turno='', laboratorio='', scroll_y='', turno_forcado=None):
    if turno_forcado:
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': turno_forcado})
    else:
        url = reverse('cronograma_semanal')
    params = []
    if data_inicio:
        params.append(f'data_inicio={data_inicio}')
    if turno and not turno_forcado:
        params.append(f'turno={turno}')
    if laboratorio:
        params.append(f'laboratorio={laboratorio}')
    if scroll_y:
        params.append(f'scroll_y={scroll_y}')
    if params:
        url = f'{url}?{"&".join(params)}'
    return url


def _horarios_laboratorio_professor_obj(professor, turno_filtro=''):
    """Horarios do professor na(s) versao(oes) ATUALMENTE ATIVA(S) do(s)
    turno(s) - nunca uma janela calculada a partir da data de hoje.

    Manha e tarde tem cronogramas ativos totalmente independentes (cada um
    com sua propria semana-base/data_inicio em SemanaCronogramaLaboratorio,
    que pode ser uma semana completamente diferente da do outro turno, e
    que so muda quando outra semana daquele turno e ativada - nunca por
    passagem de tempo). Por isso, quando nenhum turno especifico e pedido
    ("Todos"), NUNCA usamos uma unica janela de datas combinada para os
    dois turnos (isso so faria sentido se manha e tarde sempre
    compartilhassem a mesma semana-base, o que nao e garantido) - a janela
    de cada turno e resolvida separadamente via obter_cronograma_ativo(turno)
    e os agendamentos do professor sao filtrados dentro da janela correta
    do proprio turno de cada um.
    """
    if turno_filtro not in ['manha', 'tarde']:
        turno_filtro = ''

    if not professor:
        return {
            'professor': None,
            'agendamentos': [],
            'semana_inicio': None,
            'semana_fim': None,
            'turno_filtro': turno_filtro,
        }

    turnos_a_considerar = [turno_filtro] if turno_filtro else ['manha', 'tarde']
    turno_labels = dict(TURNOS_LAB)
    janela_por_turno = {}
    itens = []

    for turno in turnos_a_considerar:
        semana_inicio_turno, semana_fim_turno = obter_cronograma_ativo(turno)
        janela_por_turno[turno] = (semana_inicio_turno, semana_fim_turno)

        if not semana_inicio_turno or not semana_fim_turno:
            continue

        agendamentos_turno = AgendamentoLab.objects.filter(
            professor=professor,
            turno=turno,
            data__range=(semana_inicio_turno, semana_fim_turno),
        ).select_related('laboratorio', 'turma').order_by('data', 'horario', 'laboratorio__nome')

        for agendamento in agendamentos_turno:
            dia_en = agendamento.data.strftime('%A')
            itens.append({
                'agendamento': agendamento,
                'dia': MAPA_DIAS_LAB.get(dia_en, dia_en),
                'turno_codigo': agendamento.turno,
                'turno': turno_labels.get(agendamento.turno, agendamento.turno),
                'aula': f'Aula{agendamento.horario}',
            })

    # Mesma ordem final que a consulta unica antiga produzia: data, turno,
    # horario, nome do laboratorio.
    itens.sort(key=lambda item: (
        item['agendamento'].data, item['turno_codigo'], item['agendamento'].horario,
        item['agendamento'].laboratorio.nome,
    ))

    if turno_filtro:
        semana_inicio, semana_fim = janela_por_turno.get(turno_filtro, (None, None))
    else:
        # So o texto informativo do cabecalho ("Semana de X a Y") quando
        # nenhum turno foi filtrado - nao afeta quais agendamentos aparecem
        # (cada um ja usou a janela do seu proprio turno acima). Preferimos
        # a janela da manha; se ela nao existir, cai para a da tarde. Se as
        # janelas dos dois turnos divergirem, o cabecalho mostra so uma
        # delas (escolha deliberadamente simples e cosmetica - nao afeta a
        # lista de agendamentos exibida).
        semana_inicio, semana_fim = janela_por_turno.get('manha', (None, None))
        if not semana_inicio:
            semana_inicio, semana_fim = janela_por_turno.get('tarde', (None, None))

    return {
        'professor': professor,
        'agendamentos': itens,
        'semana_inicio': semana_inicio,
        'semana_fim': semana_fim,
        'turno_filtro': turno_filtro,
    }


def _horarios_laboratorio_professor(user, turno_filtro=''):
    professor = Professor.objects.filter(usuario=user, ativo=True).first()
    return _horarios_laboratorio_professor_obj(professor, turno_filtro)


def _semana_geral_completa_laboratorios(turno=''):
    """Ultimo fallback de obter_cronograma_ativo(): so e usado quando NAO
    existe nenhuma linha de ConfiguracaoCronogramaLaboratorio (instalacao
    nunca configurada, nem pela Fase 1/2 nem pelo mecanismo antigo) - tenta
    adivinhar uma semana atual razoavel a partir dos proprios AgendamentoLab
    ja cadastrados.

    Quando um turno especifico e informado ('manha'/'tarde'), a busca
    considera somente agendamentos daquele turno - a semana mais recente
    que tiver ao menos um agendamento dele (mesma logica ja usada em
    _data_inicio_cronograma). Isso e o correto para qualquer chamador que
    ja pediu um turno especifico (a pagina "Meus Horarios" do professor,
    a consulta publica, etc.) - exigir que o OUTRO turno tambem tenha
    agendamento em algum lugar do sistema, como a versao antiga desta
    funcao fazia incondicionalmente, nao tem relacao com "qual semana
    devo considerar para ESTE turno" e escondia agendamentos reais.

    Sem turno informado (compatibilidade com chamadas genericas, ex. a
    rota de cronograma sem filtro), mantem o comportamento antigo: exige
    uma semana onde AMBOS os turnos tenham pelo menos um agendamento.
    """
    agendamentos_considerados = _agendamentos_labs_considerados()

    if turno in ('manha', 'tarde'):
        data_recente = agendamentos_considerados.filter(turno=turno).aggregate(
            data=Max('data')
        )['data']
        if not data_recente:
            return None, None
        semana_inicio = _inicio_semana(data_recente)
        return semana_inicio, semana_inicio + timedelta(days=6)

    datas = agendamentos_considerados.values_list('data', flat=True).order_by('-data').distinct()
    semanas_verificadas = set()

    for data in datas:
        semana_inicio = _inicio_semana(data)
        if semana_inicio in semanas_verificadas:
            continue

        semanas_verificadas.add(semana_inicio)
        semana_fim = semana_inicio + timedelta(days=6)
        turnos = set(
            agendamentos_considerados.filter(
                data__range=(semana_inicio, semana_fim)
            ).values_list('turno', flat=True)
        )

        if {'manha', 'tarde'}.issubset(turnos):
            return semana_inicio, semana_fim

    return None, None


def _semana_laboratorio_completa(data_inicio, data_fim):
    turnos = set(
        _agendamentos_labs_considerados().filter(
            data__range=(data_inicio, data_fim)
        ).values_list('turno', flat=True)
    )
    return {'manha', 'tarde'}.issubset(turnos)


def _semana_laboratorio_tem_agendamento(data_inicio, data_fim):
    return _agendamentos_labs_considerados().filter(data__range=(data_inicio, data_fim)).exists()


def _horarios_laboratorio_professor_publico(professor):
    hoje = date.today()
    mes_agendamento = f'{MESES_PT_BR[hoje.month]}/{hoje.year}'
    dias_semana = [
        {'nome': 'Segunda-feira', 'titulo': 'SEGUNDA-FEIRA', 'agendamentos': []},
        {'nome': 'Terca-feira', 'titulo': 'TERÇA-FEIRA', 'agendamentos': []},
        {'nome': 'Quarta-feira', 'titulo': 'QUARTA-FEIRA', 'agendamentos': []},
        {'nome': 'Quinta-feira', 'titulo': 'QUINTA-FEIRA', 'agendamentos': []},
        {'nome': 'Sexta-feira', 'titulo': 'SEXTA-FEIRA', 'agendamentos': []},
    ]
    dias_display = {
        'Segunda-feira': 'Segunda-feira',
        'Terca-feira': 'Terça-feira',
        'Quarta-feira': 'Quarta-feira',
        'Quinta-feira': 'Quinta-feira',
        'Sexta-feira': 'Sexta-feira',
    }

    if not professor:
        return {
            'professor': None,
            'agendamentos': [],
            'dias_semana': dias_semana,
            'mes_agendamento': mes_agendamento,
            'turno_filtro': '',
        }

    filtro_manha, _identidade_manha = _versao_ativa_turno('manha')
    filtro_tarde, _identidade_tarde = _versao_ativa_turno('tarde')

    if filtro_manha is None and filtro_tarde is None:
        return {
            'professor': professor,
            'agendamentos': [],
            'dias_semana': dias_semana,
            'mes_agendamento': mes_agendamento,
            'turno_filtro': '',
        }

    filtros_periodo = Q()

    if filtro_manha is not None:
        filtros_periodo |= filtro_manha

    if filtro_tarde is not None:
        filtros_periodo |= filtro_tarde

    agendamentos = AgendamentoLab.objects.filter(
        filtros_periodo,
        professor=professor,
    ).select_related(
        'laboratorio', 'turma'
    )

    turno_labels = {
        'manha': 'Manhã',
        'tarde': 'Tarde',
    }
    ordem_turnos = {
        'manha': 1,
        'tarde': 2,
    }

    def ordem_horario(valor):
        digitos = ''.join(caractere for caractere in str(valor) if caractere.isdigit())
        return int(digitos) if digitos else 999

    agendamentos = sorted(
        agendamentos,
        key=lambda agendamento: (
            ordem_turnos.get(agendamento.turno, 999),
            ordem_horario(agendamento.horario),
            agendamento.data,
            agendamento.laboratorio.nome,
        )
    )

    itens = []
    dias_por_nome = {dia['nome']: dia for dia in dias_semana}

    for agendamento in agendamentos:
        dia_en = agendamento.data.strftime('%A')
        dia = MAPA_DIAS_LAB.get(dia_en, dia_en)
        item = {
            'agendamento': agendamento,
            'dia': dias_display.get(dia, dia),
            'turno_codigo': agendamento.turno,
            'turno': turno_labels.get(agendamento.turno, agendamento.turno),
            'aula': f'Aula{agendamento.horario}',
        }
        itens.append(item)

        if dia in dias_por_nome:
            dias_por_nome[dia]['agendamentos'].append(item)

    return {
        'professor': professor,
        'agendamentos': itens,
        'dias_semana': dias_semana,
        'mes_agendamento': mes_agendamento,
        'turno_filtro': '',
    }


_VERSAO_HORARIO_PUBLICO_SENTINELA = hashlib.sha256(b'consulta-publica-lab-professor-invalido').hexdigest()[:16]


def calcular_versao_horario_publico(professor):
    """Calcula um hash curto que muda sempre que o horário público exibido para o
    professor (ou a semana ativa do cronograma) muda. Usa apenas os campos
    exibidos na página de consulta pública, via values_list (sem instanciar
    objetos, uma única query simples). Não expõe nenhum dado do professor.
    """
    if not professor or not professor.ativo:
        return _VERSAO_HORARIO_PUBLICO_SENTINELA

    filtro_manha, identidade_manha = _versao_ativa_turno('manha')
    filtro_tarde, identidade_tarde = _versao_ativa_turno('tarde')

    filtros_periodo = Q()

    if filtro_manha is not None:
        filtros_periodo |= filtro_manha

    if filtro_tarde is not None:
        filtros_periodo |= filtro_tarde

    if filtros_periodo:
        agendamentos = list(
            AgendamentoLab.objects.filter(
                filtros_periodo,
                professor=professor,
            ).order_by('id').values_list(
                'data', 'horario', 'turno', 'disciplina', 'observacao',
                'laboratorio__nome', 'turma__nome',
            )
        )
    else:
        agendamentos = []

    payload = repr((
        professor.ativo,
        identidade_manha,
        identidade_tarde,
        agendamentos,
    ))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]


def _disponibilidade_dispositivos_publica():
    manha_inicio, manha_fim = obter_cronograma_ativo('manha')
    tarde_inicio, tarde_fim = obter_cronograma_ativo('tarde')

    if not manha_inicio and not tarde_inicio:
        return []

    tipos_dispositivos = {
        '4': 'Tablets',
        '5': 'Celulares',
    }
    laboratorios = {}
    for laboratorio in _laboratorios_agendaveis():
        numero = ''.join(caractere for caractere in laboratorio.nome if caractere.isdigit())
        if numero:
            laboratorios[str(int(numero))] = laboratorio

    laboratorios_dispositivos = [
        (numero, tipos_dispositivos[numero], laboratorios[numero])
        for numero in tipos_dispositivos
        if numero in laboratorios
    ]
    if not laboratorios_dispositivos:
        return []

    filtros_periodo = Q()
    if manha_inicio and manha_fim:
        filtros_periodo |= Q(
            turno='manha',
            data__range=(manha_inicio, manha_fim),
        )
    if tarde_inicio and tarde_fim:
        filtros_periodo |= Q(
            turno='tarde',
            data__range=(tarde_inicio, tarde_fim),
        )

    agendamentos_ocupados = AgendamentoLab.objects.filter(
        filtros_periodo,
        laboratorio__in=[laboratorio for _, _, laboratorio in laboratorios_dispositivos],
    ).values_list('laboratorio_id', 'data', 'turno', 'horario')

    agendamentos_por_slot = {
        (laboratorio_id, data, turno, horario)
        for laboratorio_id, data, turno, horario in agendamentos_ocupados
    }

    dias_display = {
        'Segunda-feira': 'SEGUNDA-FEIRA',
        'Terca-feira': 'TERÇA-FEIRA',
        'Quarta-feira': 'QUARTA-FEIRA',
        'Quinta-feira': 'QUINTA-FEIRA',
        'Sexta-feira': 'SEXTA-FEIRA',
    }

    disponibilidade = []

    periodos = []
    if manha_inicio and manha_fim:
        periodos.append(('manha', 'Manha', manha_inicio, manha_fim))
    if tarde_inicio and tarde_fim:
        periodos.append(('tarde', 'Tarde', tarde_inicio, tarde_fim))

    for _, tipo_dispositivo, laboratorio in laboratorios_dispositivos:
        dias = []

        for turno, turno_label, data_inicio, data_fim in periodos:
            data = data_inicio
            while data <= data_fim:
                dia_nome = MAPA_DIAS_LAB.get(data.strftime('%A'), data.strftime('%A'))

                if dia_nome in dias_display:
                    slots = []

                    for horario in HORARIOS_LAB:
                        if (laboratorio.id, data, turno, horario) not in agendamentos_por_slot:
                            slots.append({
                                'aula': f'Aula {horario}',
                                'turno': turno_label,
                            })

                    if slots:
                        dias.append({
                            'titulo': f"{dias_display[dia_nome]} - {turno_label}",
                            'slots': slots,
                        })

                data += timedelta(days=1)

        disponibilidade.append({
            'laboratorio': laboratorio,
            'tipo_dispositivo': tipo_dispositivo,
            'dias': dias,
        })

    return disponibilidade

@login_required
def meus_horarios_laboratorio(request):
    context = {
        'horarios_laboratorio_professor': _horarios_laboratorio_professor(
            request.user,
            request.GET.get('turno', ''),
        ),
    }
    return render(request, 'laboratorios/meus_horarios.html', context)


def consulta_publica_professor_laboratorio(request):
    if getattr(settings, 'CONSULTA_LABORATORIO_EM_MANUTENCAO', False):
        return render(request, 'laboratorios/consulta_professor_manutencao.html')

    professor_id = request.GET.get('professor_id', '').strip()
    professor = None
    if professor_id.isdigit():
        professor = Professor.objects.filter(id=professor_id, ativo=True).first()

    context = {
        'professor_id': professor_id if professor else '',
        'horarios_laboratorio_professor': _horarios_laboratorio_professor_publico(professor),
        'versao_horario_publico': calcular_versao_horario_publico(professor),
    }
    return render(request, 'laboratorios/consulta_professor_publica.html', context)


@require_GET
def versao_horario_professor_laboratorio_publico(request):
    """Endpoint minimo e somente-leitura usado pelo polling da pagina publica
    para saber se o horario do professor mudou. Nunca retorna horarios, listas
    ou qualquer dado do professor - apenas um hash de versao."""
    professor_id = request.GET.get('professor_id', '').strip()
    professor = None
    if professor_id.isdigit():
        professor = Professor.objects.filter(id=professor_id, ativo=True).first()

    return JsonResponse({'versao': calcular_versao_horario_publico(professor)})


def disponibilidade_tablets_celulares_publica(request):
    return render(request, 'laboratorios/disponibilidade_tablets_celulares.html', {
        'disponibilidade_dispositivos': _disponibilidade_dispositivos_publica(),
    })


def autocomplete_professores_laboratorio(request):
    termo = request.GET.get('q', '').strip()
    professores = Professor.objects.none()

    if termo:
        professores = Professor.objects.filter(
            ativo=True,
            nome_completo__icontains=termo,
        ).order_by('nome_completo')[:10]

    resultados = [
        {
            'id': professor.id,
            'nome': professor.nome_completo,
        }
        for professor in professores
    ]
    return JsonResponse({'resultados': resultados})

@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def listar_laboratorios(request):
    """Lista todos os laboratorios"""
    laboratorios = Laboratorio.objects.filter(ativo=True)

    # Contagem de disponiveis para itinerantes
    for lab in laboratorios:
        if lab.tipo == 'itinerante':
            lab.disponiveis = ItemEquipamento.objects.filter(
                laboratorio=lab,
                disponivel=True
            ).count()

    return render(request, 'laboratorios/listar.html', {'laboratorios': laboratorios})


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def diagnostico_cronograma_laboratorios(request):
    """Exibe um diagnostico semanal dos agendamentos de laboratorios."""
    configuracao_cronograma = ConfiguracaoCronogramaLaboratorio.objects.first()
    agendamentos = _agendamentos_labs_considerados().select_related(
        'professor', 'laboratorio', 'turma'
    ).order_by('-data', 'turno', 'horario')

    semanas = {}
    data_mais_recente = None
    data_mais_antiga = None

    for agendamento in agendamentos:
        semana_inicio = _inicio_semana(agendamento.data)
        semana_fim = semana_inicio + timedelta(days=6)

        if data_mais_recente is None or agendamento.data > data_mais_recente:
            data_mais_recente = agendamento.data
        if data_mais_antiga is None or agendamento.data < data_mais_antiga:
            data_mais_antiga = agendamento.data

        semana = semanas.setdefault(semana_inicio, {
            'data_inicio': semana_inicio,
            'data_fim': semana_fim,
            'total': 0,
            'manha': 0,
            'tarde': 0,
            'professores': set(),
            'laboratorios': set(),
            'turmas': set(),
        })

        semana['total'] += 1
        if agendamento.turno == 'manha':
            semana['manha'] += 1
        elif agendamento.turno == 'tarde':
            semana['tarde'] += 1

        if agendamento.professor_id:
            semana['professores'].add(agendamento.professor_id)
        if agendamento.laboratorio_id:
            semana['laboratorios'].add(agendamento.laboratorio_id)
        if agendamento.turma_id:
            semana['turmas'].add(agendamento.turma_id)

    cronograma_configurado = configuracao_cronograma is not None
    semana_ativa_manha_inicio, semana_ativa_manha_fim = obter_cronograma_ativo('manha')
    semana_ativa_tarde_inicio, semana_ativa_tarde_fim = obter_cronograma_ativo('tarde')
    semana_ativa_manha_obj = _semana_ativa_obj('manha')
    semana_ativa_tarde_obj = _semana_ativa_obj('tarde')
    semanas_historicas_manha = list(_semanas_historicas_qs('manha'))
    semanas_historicas_tarde = list(_semanas_historicas_qs('tarde'))
    semanas_canceladas_manha = list(_semanas_canceladas_qs('manha'))
    semanas_canceladas_tarde = list(_semanas_canceladas_qs('tarde'))

    # Fase 2: SemanaCronogramaLaboratorio e a fonte unica das listas do
    # Diagnostico. A consulta e direta e independe de marcadores legados,
    # sessao ou existencia de agendamentos.
    semanas_editando_manha = list(_semanas_editando_qs('manha'))
    semana_editando_manha_inicio = semanas_editando_manha[0].data_inicio if semanas_editando_manha else None
    semana_editando_manha_fim = semana_editando_manha_inicio + timedelta(days=6) if semana_editando_manha_inicio else None

    semanas_editando_tarde = list(_semanas_editando_qs('tarde'))
    semana_editando_tarde_inicio = semanas_editando_tarde[0].data_inicio if semanas_editando_tarde else None
    semana_editando_tarde_fim = semana_editando_tarde_inicio + timedelta(days=6) if semana_editando_tarde_inicio else None

    diagnostico = []
    if data_mais_recente and data_mais_antiga:
        semana_cursor = _inicio_semana(data_mais_recente)
        semana_limite = _inicio_semana(data_mais_antiga)

        while semana_cursor >= semana_limite:
            semana = semanas.get(semana_cursor, {
                'data_inicio': semana_cursor,
                'data_fim': semana_cursor + timedelta(days=6),
                'total': 0,
                'manha': 0,
                'tarde': 0,
                'professores': set(),
                'laboratorios': set(),
                'turmas': set(),
            })

            tem_manha = semana['manha'] > 0
            tem_tarde = semana['tarde'] > 0
            semana_completa = tem_manha and tem_tarde
            semana_parcial = (tem_manha or tem_tarde) and not semana_completa
            semana_ativavel = semana_completa or semana_parcial
            cronograma_ativo_manha = semana_cursor == semana_ativa_manha_inicio
            cronograma_ativo_tarde = semana_cursor == semana_ativa_tarde_inicio
            cronograma_ativo = cronograma_ativo_manha or cronograma_ativo_tarde

            if semana_completa:
                status = 'Completo'
                status_icone = '✅'
                status_classe = 'success'
            elif semana_parcial:
                status = 'Parcial'
                status_icone = '⚠️'
                status_classe = 'warning'
            else:
                status = 'Sem agendamentos'
                status_icone = '❌'
                status_classe = 'danger'

            diagnostico.append({
                'data_inicio': semana['data_inicio'],
                'data_fim': semana['data_fim'],
                'total': semana['total'],
                'manha': semana['manha'],
                'tarde': semana['tarde'],
                'professores_total': len(semana['professores']),
                'laboratorios_total': len(semana['laboratorios']),
                'turmas_total': len(semana['turmas']),
                'status': status,
                'status_icone': status_icone,
                'status_classe': status_classe,
                'semana_completa': semana_completa,
                'semana_parcial': semana_parcial,
                'semana_ativavel': semana_ativavel,
                'cronograma_ativo': cronograma_ativo,
                'cronograma_ativo_manha': cronograma_ativo_manha,
                'cronograma_ativo_tarde': cronograma_ativo_tarde,
                'cronograma_configurado': cronograma_ativo and cronograma_configurado,
            })

            semana_cursor -= timedelta(days=7)

    context = {
        'semanas': diagnostico,
        'semana_ativa_manha_inicio': semana_ativa_manha_inicio,
        'semana_ativa_manha_fim': semana_ativa_manha_fim,
        'semana_ativa_tarde_inicio': semana_ativa_tarde_inicio,
        'semana_ativa_tarde_fim': semana_ativa_tarde_fim,
        'semana_ativa_manha_obj': semana_ativa_manha_obj,
        'semana_ativa_tarde_obj': semana_ativa_tarde_obj,
        'semana_editando_manha_inicio': semana_editando_manha_inicio,
        'semana_editando_manha_fim': semana_editando_manha_fim,
        'semana_editando_tarde_inicio': semana_editando_tarde_inicio,
        'semana_editando_tarde_fim': semana_editando_tarde_fim,
        # Fase 2: listas completas (sem limite), usadas pelo novo template de
        # cartoes multiplos por turno.
        'semanas_editando_manha': semanas_editando_manha,
        'semanas_editando_tarde': semanas_editando_tarde,
        'semanas_historicas_manha': semanas_historicas_manha,
        'semanas_historicas_tarde': semanas_historicas_tarde,
        'semanas_canceladas_manha': semanas_canceladas_manha,
        'semanas_canceladas_tarde': semanas_canceladas_tarde,
        'cronograma_configurado': cronograma_configurado,
        'configuracao_cronograma': configuracao_cronograma,
    }
    return render(request, 'laboratorios/diagnostico_cronograma.html', context)


@login_required
@user_passes_test(pode_ver_cronograma_laboratorios, login_url='/')
def consulta_cronograma_laboratorios(request):
    """Entrada de consulta SOMENTE LEITURA ao cronograma dos laboratorios,
    pensada para o grupo "Consulta Cronograma" (tambem acessivel por Equipe
    Diretiva/superuser, que normalmente usam o Diagnostico administrativo
    em vez desta tela). Mostra a semana ativa de cada turno e a lista de
    semanas historicas, cada uma linkando direto para a grade real
    (cronograma_semanal_turno, que ja faz toda a renderizacao e ja e
    somente leitura para quem nao e Equipe Diretiva) - nao duplica a logica
    da grade aqui, so organiza os links de entrada.

    Nenhum botao de escrita (Criar semana, Editar, Ativar, Cancelar,
    Excluir) existe nesta tela ou na grade para quem chega por aqui sem ser
    Equipe Diretiva - a protecao real, de qualquer forma, esta no backend
    (ver pertence_ao_grupo_equipe_diretiva em cronograma_semanal e nas
    views de ativar/desativar/criar/cancelar/excluir semana).
    """
    turnos = []
    for turno, rotulo in (('manha', 'Manhã'), ('tarde', 'Tarde')):
        ativa_inicio, ativa_fim = obter_cronograma_ativo(turno)
        historicas = list(
            SemanaCronogramaLaboratorio.objects.filter(turno=turno, status='historica').order_by('-data_inicio')
        )
        turnos.append({
            'turno': turno,
            'rotulo': rotulo,
            'ativa_inicio': ativa_inicio,
            'ativa_fim': ativa_fim,
            'historicas': historicas,
            'url_turno': reverse('cronograma_semanal_turno', kwargs={'turno_forcado': turno}),
        })

    return render(request, 'laboratorios/consulta_cronograma.html', {'turnos': turnos})


def _ativar_legado(request, turno, semana_inicio, semana_fim):
    """Caminho de compatibilidade: turno ainda nao tem nenhuma linha em
    SemanaCronogramaLaboratorio ('nao adotado') no momento deste POST.

    Sincroniza os campos antigos (igual a Fase 1/anterior) e TAMBEM cria a
    linha 'ativa' correspondente na tabela nova, adotando o turno nesse
    mesmo instante. Sem isso, uma ativacao registrada so nos campos
    legados ficaria "invisivel" assim que o turno fosse adotado por outro
    caminho (ex.: 'Criar semana' para uma data diferente) - o momento em
    que _turno_usa_semana_nova(turno) passa a ser True, a tabela nova se
    torna a UNICA fonte para aquele turno (ver obter_cronograma_ativo), e
    uma ativacao legada nunca migrada para la deixaria de ser encontrada.
    Criar a linha aqui evita esse estado inconsistente.
    """
    if not _agendamentos_labs_considerados().filter(
        turno=turno,
        data__range=(semana_inicio, semana_fim),
    ).exists():
        messages.error(request, f'Nao existem agendamentos no turno {turno} nesta semana.')
        return redirect('diagnostico_cronograma_laboratorios')

    usuario = request.user if request.user.is_authenticated else None

    with transaction.atomic():
        configuracao = ConfiguracaoCronogramaLaboratorio.objects.first()
        if not configuracao:
            configuracao = ConfiguracaoCronogramaLaboratorio.objects.create(
                data_inicio_cronograma_ativo=semana_inicio,
                data_fim_cronograma_ativo=semana_fim,
                atualizado_por=usuario,
            )

        if turno == 'manha':
            configuracao.data_inicio_manha_ativo = semana_inicio
            configuracao.data_fim_manha_ativo = semana_fim
        else:
            configuracao.data_inicio_tarde_ativo = semana_inicio
            configuracao.data_fim_tarde_ativo = semana_fim

        configuracao.atualizado_por = usuario
        configuracao.save()
        ConfiguracaoCronogramaLaboratorio.objects.exclude(id=configuracao.id).delete()

        SemanaCronogramaLaboratorio.objects.create(
            turno=turno,
            data_inicio=semana_inicio,
            status='ativa',
            criado_por=usuario,
            ativado_por=usuario,
            ativado_em=timezone.now(),
        )

    messages.success(request, f'Cronograma da {turno} atualizado com sucesso.')
    return redirect('diagnostico_cronograma_laboratorios')


def _ativar_semana_por_pk(request, semana):
    """Ativa atomicamente UMA semana (objeto SemanaCronogramaLaboratorio) que
    esteja em edicao ou no historico: a ativa anterior do mesmo turno (se
    houver) vira historica, as demais versoes permanecem intactas, e o outro
    turno nunca e tocado. Sincroniza os campos antigos *_ativo (so os daquele
    turno) como compatibilidade temporaria."""
    if semana.status == 'ativa':
        messages.info(request, 'Esta semana ja esta ativa.')
        return redirect('diagnostico_cronograma_laboratorios')

    if semana.status not in ('editando', 'historica'):
        messages.error(
            request,
            'Somente uma semana em edicao ou historica pode ser ativada.'
        )
        return redirect('diagnostico_cronograma_laboratorios')

    turno = semana.turno
    usuario = request.user if request.user.is_authenticated else None
    agora = timezone.now()

    try:
        with transaction.atomic():
            SemanaCronogramaLaboratorio.objects.filter(
                turno=turno, status='ativa'
            ).exclude(id=semana.id).update(status='historica')

            # UPDATE condicional atomico pela PK e pelo status permitido.
            linhas = SemanaCronogramaLaboratorio.objects.filter(
                id=semana.id, status__in=('editando', 'historica')
            ).update(status='ativa', ativado_por=usuario, ativado_em=agora)

            if not linhas:
                raise IntegrityError('Semana alterada concorrentemente.')

            _sincronizar_campo_ativo_antigo(turno, semana.data_inicio, semana.data_fim)
    except IntegrityError:
        messages.error(
            request,
            'Nao foi possivel ativar esta semana. O estado do turno pode ter mudado '
            'ou ja existe uma versao em edicao para a mesma data. Nenhuma alteracao '
            'foi aplicada; recarregue a pagina e tente novamente.'
        )
        return redirect('diagnostico_cronograma_laboratorios')

    messages.success(
        request,
        f'Semana da {turno} iniciada em {semana.data_inicio.strftime("%d/%m/%Y")} agora esta ATIVA.'
    )
    return redirect('diagnostico_cronograma_laboratorios')


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
@require_POST
def ativar_cronograma_laboratorio(request):
    """Ativa uma semana especifica de um turno, sem alterar o outro turno.

    Fase 2: o formulario novo (por cartao, um por semana em edicao) envia
    'semana_id' - a identidade exata da linha de SemanaCronogramaLaboratorio
    a ativar, sem nenhuma tentativa de 'adivinhar' qual semana ativar. Para
    compatibilidade, um POST antigo com 'turno'+'data_inicio' (sem
    semana_id) ainda funciona: se o turno ja tiver adotado a tabela nova,
    resolve para a linha 'editando' com aquela data; senao, cai no
    comportamento legado (Fase 1 e anteriores), intacto.
    """
    semana_id = request.POST.get('semana_id', '').strip()

    if semana_id:
        try:
            semana = SemanaCronogramaLaboratorio.objects.get(id=semana_id)
        except (SemanaCronogramaLaboratorio.DoesNotExist, ValueError, TypeError):
            messages.error(request, 'Semana nao encontrada.')
            return redirect('diagnostico_cronograma_laboratorios')
        return _ativar_semana_por_pk(request, semana)

    turno = request.POST.get('turno', '').strip()
    data_inicio = request.POST.get('data_inicio', '').strip()

    if turno not in ['manha', 'tarde']:
        messages.error(request, 'Turno invalido.')
        return redirect('diagnostico_cronograma_laboratorios')

    try:
        data_inicio_obj = datetime.strptime(data_inicio, '%Y-%m-%d').date()
    except ValueError:
        messages.error(request, 'Data invalida para ativar o cronograma.')
        return redirect('diagnostico_cronograma_laboratorios')

    if not _turno_usa_semana_nova(turno):
        # Compatibilidade legada: sem uma versao persistida, o periodo ainda
        # era organizado em semanas civis iniciadas na segunda-feira.
        semana_inicio = _inicio_semana(data_inicio_obj)
        semana_fim = semana_inicio + timedelta(days=6)
        return _ativar_legado(request, turno, semana_inicio, semana_fim)

    # Na estrutura nova, a data informada e a identidade exata da versao.
    semana = _semana_por_turno_e_data(turno, data_inicio_obj)
    if semana is None:
        messages.error(request, 'Semana nao encontrada.')
        return redirect('diagnostico_cronograma_laboratorios')
    return _ativar_semana_por_pk(request, semana)


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
@require_POST
def desativar_cronograma_laboratorio(request):
    """Desativa o cronograma do turno informado, sem afetar o outro turno.

    Fase 2 (turno ja adotado): a semana ATIVA passa a HISTORICA (nunca e
    apagada) e NENHUMA semana em edicao e promovida automaticamente - a
    equipe escolhe explicitamente qual ativar depois, via 'Ativar'.
    Compatibilidade (turno ainda nao adotado): comportamento legado intacto,
    so limpa os campos antigos *_ativo daquele turno."""
    turno = request.POST.get('turno', '').strip()
    if turno not in ['manha', 'tarde']:
        messages.error(request, 'Turno invalido.')
        return redirect('diagnostico_cronograma_laboratorios')

    if _turno_usa_semana_nova(turno):
        with transaction.atomic():
            atualizados = SemanaCronogramaLaboratorio.objects.filter(
                turno=turno, status='ativa'
            ).update(status='historica')
            if atualizados:
                _limpar_campo_ativo_antigo(turno)
        if atualizados:
            messages.success(request, f'Cronograma da {turno} foi desativado (semana movida para historico).')
        else:
            messages.info(request, f'Nao havia semana ativa na {turno}.')
        return redirect('diagnostico_cronograma_laboratorios')

    configuracao = ConfiguracaoCronogramaLaboratorio.objects.first()

    if configuracao:
        if turno == 'manha':
            configuracao.data_inicio_manha_ativo = None
            configuracao.data_fim_manha_ativo = None
        else:
            configuracao.data_inicio_tarde_ativo = None
            configuracao.data_fim_tarde_ativo = None

        configuracao.atualizado_por = request.user if request.user.is_authenticated else None
        configuracao.save()

    messages.success(
        request,
        f'Cronograma da {turno} foi limpo.'
    )
    return redirect('diagnostico_cronograma_laboratorios')


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def editar_semana_cronograma_laboratorio(request):
    """Cria uma nova semana EM EDICAO para um turno (Fase 2: 'Criar semana').

    Substitui o antigo marcador unico compartilhado
    (data_inicio_<turno>_editando, que so guardava 1 semana por turno): a
    partir de agora existe uma linha propria em SemanaCronogramaLaboratorio
    por semana em edicao, sem limite de quantidade. Continua sem ativar a
    semana e sem afetar o outro turno. Bloqueia duplicidade quando ja existe
    QUALQUER linha (ativa, editando, historica ou cancelada) para o mesmo
    turno+data - reativar historica/cancelada automaticamente exigiria uma
    regra explicita ainda nao definida (ver secao 8 do relatorio da Fase 2).
    """
    if request.method != 'POST':
        messages.error(request, 'A preparacao da semana deve ser realizada pelo formulario.')
        return redirect('diagnostico_cronograma_laboratorios')

    semana_id = request.POST.get('semana_id', '').strip()
    if semana_id:
        try:
            with transaction.atomic():
                semana = SemanaCronogramaLaboratorio.objects.select_for_update().get(id=semana_id)

                if semana.status != 'historica':
                    messages.error(request, 'Somente uma versao historica pode voltar para edicao.')
                    return redirect('diagnostico_cronograma_laboratorios')

                conflito = SemanaCronogramaLaboratorio.objects.filter(
                    turno=semana.turno,
                    data_inicio=semana.data_inicio,
                ).exclude(id=semana.id).exists()
                if conflito:
                    messages.error(
                        request,
                        'Nao foi possivel editar esta versao: ja existe outra versao '
                        'para o mesmo turno e data. Nenhuma alteracao foi aplicada.',
                    )
                    return redirect('diagnostico_cronograma_laboratorios')

                atualizadas = SemanaCronogramaLaboratorio.objects.filter(
                    id=semana.id, status='historica',
                ).update(status='editando')
                if not atualizadas:
                    raise IntegrityError('A versao historica foi alterada concorrentemente.')
        except (SemanaCronogramaLaboratorio.DoesNotExist, ValueError, TypeError):
            messages.error(request, 'Semana nao encontrada.')
            return redirect('diagnostico_cronograma_laboratorios')
        except IntegrityError:
            messages.error(
                request,
                'Nao foi possivel colocar esta versao em edicao. Nenhuma alteracao foi aplicada; '
                'recarregue a pagina e tente novamente.',
            )
            return redirect('diagnostico_cronograma_laboratorios')

        messages.success(
            request,
            f'Versao da {semana.turno} iniciada em '
            f'{semana.data_inicio.strftime("%d/%m/%Y")} agora esta EM EDICAO.',
        )
        return redirect('diagnostico_cronograma_laboratorios')

    turno = request.POST.get('turno', '').strip()
    data_inicio = request.POST.get('data_inicio', '').strip()

    if turno not in ['manha', 'tarde']:
        messages.error(request, 'Turno invalido.')
        return redirect('diagnostico_cronograma_laboratorios')

    try:
        semana_inicio = datetime.strptime(data_inicio, '%Y-%m-%d').date()
    except ValueError:
        messages.error(request, 'Data invalida para editar a semana.')
        return redirect('diagnostico_cronograma_laboratorios')

    existente = _semana_por_turno_e_data(turno, semana_inicio)
    if existente:
        mensagens_status = {
            'ativa': 'Esta semana ja esta ATIVA neste turno.',
            'editando': 'Esta semana ja esta EM EDICAO neste turno.',
            'historica': 'Esta semana e HISTORICA neste turno e nao pode ser recriada automaticamente.',
            'cancelada': 'Esta semana esta CANCELADA neste turno e nao pode ser recriada automaticamente.',
        }
        messages.error(request, mensagens_status.get(existente.status, 'Esta semana ja existe neste turno.'))
        return redirect('diagnostico_cronograma_laboratorios')

    SemanaCronogramaLaboratorio.objects.create(
        turno=turno,
        data_inicio=semana_inicio,
        status='editando',
        criado_por=request.user if request.user.is_authenticated else None,
    )

    messages.success(
        request,
        f'Semana com inicio em {semana_inicio.strftime("%d/%m/%Y")} '
        f'esta em edicao no turno da {turno}. Confira os agendamentos e depois clique em Ativar.'
    )
    return redirect('diagnostico_cronograma_laboratorios')


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def cancelar_edicao_semana_cronograma_laboratorio(request):
    """Cancela uma semana em edicao: editando -> cancelada. NAO apaga nenhum
    AgendamentoLab - eles continuam vinculados aquela semana para auditoria.

    Fase 2: o formulario novo (por cartao) envia 'semana_id' - cancela
    exclusivamente aquela linha. Compatibilidade: um POST antigo so com
    'turno' (sem semana_id) cancela todas as semanas em edicao daquele
    turno - equivalente ao comportamento antigo quando havia no maximo uma."""
    if request.method != 'POST':
        messages.error(request, 'A operacao deve ser realizada pelo formulario.')
        return redirect('diagnostico_cronograma_laboratorios')

    semana_id = request.POST.get('semana_id', '').strip()

    if semana_id:
        try:
            semana = SemanaCronogramaLaboratorio.objects.get(id=semana_id)
        except (SemanaCronogramaLaboratorio.DoesNotExist, ValueError, TypeError):
            messages.error(request, 'Semana nao encontrada.')
            return redirect('diagnostico_cronograma_laboratorios')

        if semana.status != 'editando':
            messages.error(request, 'Somente uma semana em edicao pode ser cancelada.')
            return redirect('diagnostico_cronograma_laboratorios')

        semana.status = 'cancelada'
        semana.cancelado_por = request.user if request.user.is_authenticated else None
        semana.cancelado_em = timezone.now()
        semana.save(update_fields=['status', 'cancelado_por', 'cancelado_em'])

        messages.success(
            request,
            f'Edicao da semana de {semana.data_inicio.strftime("%d/%m/%Y")} ({semana.turno}) foi cancelada.'
        )
        return redirect('diagnostico_cronograma_laboratorios')

    turno = request.POST.get('turno', '').strip()
    if turno not in ['manha', 'tarde']:
        messages.error(request, 'Turno invalido.')
        return redirect('diagnostico_cronograma_laboratorios')

    if _turno_usa_semana_nova(turno):
        SemanaCronogramaLaboratorio.objects.filter(turno=turno, status='editando').update(
            status='cancelada',
            cancelado_por=request.user if request.user.is_authenticated else None,
            cancelado_em=timezone.now(),
        )
    else:
        # UPDATE parcial legado: so o campo "_editando" do turno informado.
        ConfiguracaoCronogramaLaboratorio.objects.update(
            **{_campo_semana_editando(turno): None}
        )
    messages.success(request, f'Edicao da {turno} foi cancelada.')
    return redirect('diagnostico_cronograma_laboratorios')


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def excluir_semana_cronograma_laboratorio(request):
    """Rota antiga, mantida somente por compatibilidade de URL.

    Correcao de seguranca (Fase 2 complementar): esta view fazia exclusao
    fisica de AgendamentoLab e havia deixado de estar linkada na interface
    desde a Fase 2 (o cartao de diagnostico novo nao oferece mais botao de
    "Excluir semana"), mas continuava acessivel por URL direta e SEM passar
    por SemanaCronogramaLaboratorio - um bypass real da protecao de
    escrita/exclusao introduzida na Fase 2. A partir de agora, esta rota
    NUNCA apaga nenhum AgendamentoLab, sob nenhuma circunstancia: so informa
    a situacao da semana pedida (ativa/editando/historica/cancelada/
    desconhecida) e redireciona. Uma semana em edicao so pode ser encerrada
    via "Cancelar edicao" (vira 'cancelada', sem apagar agendamentos); ativa,
    historica e cancelada nunca podem ser excluidas fisicamente em nenhuma
    fase atual do sistema.
    """
    if request.method != 'POST':
        messages.error(request, 'A operacao deve ser realizada pelo formulario.')
        return redirect('diagnostico_cronograma_laboratorios')

    semana_id = request.POST.get('semana_id', '').strip()
    if semana_id:
        try:
            with transaction.atomic():
                semana = SemanaCronogramaLaboratorio.objects.select_for_update().get(id=semana_id)
                atualizadas = SemanaCronogramaLaboratorio.objects.filter(
                    id=semana.id, status='historica',
                ).update(
                    status='cancelada',
                    cancelado_por=request.user if request.user.is_authenticated else None,
                    cancelado_em=timezone.now(),
                )
                if not atualizadas:
                    messages.error(request, 'Somente uma versao historica pode ser removida do Historico.')
                    return redirect('diagnostico_cronograma_laboratorios')
        except (SemanaCronogramaLaboratorio.DoesNotExist, ValueError, TypeError):
            messages.error(request, 'Semana nao encontrada.')
            return redirect('diagnostico_cronograma_laboratorios')
        except IntegrityError:
            messages.error(
                request,
                'Nao foi possivel remover esta versao do Historico. Nenhuma alteracao foi aplicada; '
                'recarregue a pagina e tente novamente.',
            )
            return redirect('diagnostico_cronograma_laboratorios')

        messages.success(
            request,
            f'Versao da {semana.turno} iniciada em '
            f'{semana.data_inicio.strftime("%d/%m/%Y")} removida do Historico. '
            'Os horarios associados foram preservados.',
        )
        return redirect('diagnostico_cronograma_laboratorios')

    turno = request.POST.get('turno', '').strip()
    data_inicio = request.POST.get('data_inicio', '').strip()

    if turno not in ['manha', 'tarde']:
        messages.error(request, 'Turno invalido.')
        return redirect('diagnostico_cronograma_laboratorios')

    try:
        data_inicio_obj = datetime.strptime(data_inicio, '%Y-%m-%d').date()
    except ValueError:
        messages.error(request, 'Data invalida.')
        return redirect('diagnostico_cronograma_laboratorios')

    turno_adotado = _turno_usa_semana_nova(turno)
    # Uma versao nova e localizada pela data exata. A normalizacao abaixo
    # fica restrita ao fallback legado, que nao possui identidade persistida.
    semana_inicio = data_inicio_obj if turno_adotado else _inicio_semana(data_inicio_obj)
    semana = _semana_por_turno_e_data(turno, semana_inicio) if turno_adotado else None

    if semana is not None:
        mensagens_por_status = {
            'editando': (
                'Esta semana esta em edicao. A exclusao fisica de agendamentos foi '
                'desativada nesta rota - use "Cancelar edicao" no diagnostico do cronograma '
                'para encerra-la sem apagar os agendamentos.'
            ),
            'ativa': 'Esta semana esta ativa e nao pode ser excluida fisicamente.',
            'historica': (
                'Esta semana e historica e nao pode ser excluida fisicamente '
                '(preservada para auditoria).'
            ),
            'cancelada': (
                'Esta semana ja esta cancelada e permanece registrada para auditoria - '
                'seus agendamentos nao sao apagados.'
            ),
        }
        messages.error(
            request,
            mensagens_por_status.get(semana.status, 'A exclusao fisica de agendamentos foi desativada.'),
        )
        return redirect('diagnostico_cronograma_laboratorios')

    # Turno ainda nao adotou SemanaCronogramaLaboratorio (compatibilidade
    # legada) ou nao ha nenhuma semana cadastrada para esta data - mesmo
    # assim, a exclusao fisica de AgendamentoLab por esta rota continua
    # incondicionalmente desativada.
    ativo_inicio, _ativo_fim = obter_cronograma_ativo(turno)
    if ativo_inicio == semana_inicio:
        messages.error(
            request,
            f'Esta semana esta ativa no turno da {turno} e nao pode ser excluida fisicamente.'
        )
    else:
        messages.error(
            request,
            'A exclusao fisica de agendamentos por esta rota foi desativada. '
            'Use "Cancelar edicao" para encerrar uma semana em edicao, se aplicavel.'
        )

    return redirect('diagnostico_cronograma_laboratorios')


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
@require_POST
def excluir_agendamento(request, agendamento_id):
    agendamento = get_object_or_404(
        AgendamentoLab.objects.select_related('semana'), id=agendamento_id,
    )
    semana = agendamento.semana
    semana_coerente = (
        semana is not None
        and semana.turno == agendamento.turno
        # A FK define a identidade; +6 dias valida somente se o agendamento
        # pertence ao intervalo operacional daquela versao.
        and semana.data_inicio <= agendamento.data <= semana.data_fim
        and semana.status in ('ativa', 'editando')
    )
    if not semana_coerente:
        messages.error(
            request,
            'Este agendamento nao pertence a uma semana ativa ou em edicao e nao pode ser excluido.',
        )
        return redirect('diagnostico_cronograma_laboratorios')

    agendamento.delete()
    messages.success(request, "Agendamento removido com sucesso!")
    data_inicio = request.POST.get('data_inicio', '')
    turno = request.POST.get('turno', '')
    laboratorio = request.POST.get('laboratorio', '')
    scroll_y = request.POST.get('scroll_y', '')
    turno_bloqueado = request.POST.get('turno_bloqueado', '')
    turno_bloqueado = turno_bloqueado if turno_bloqueado in ('manha', 'tarde') else None
    return redirect(_url_cronograma_com_filtros(
        data_inicio, turno, laboratorio, scroll_y, turno_forcado=turno_bloqueado
    ))

@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def agendamento_lab(request, lab_id):
    """Agendar laboratorio por aula (Labs 01 a 05)."""
    laboratorio = get_object_or_404(Laboratorio, id=lab_id, ativo=True)
    cronograma_ativo_inicio, cronograma_ativo_fim = _periodo_cronograma(request)
    dias_semana_agendamento = [
        {'valor': '0', 'nome': 'Segunda-feira'},
        {'valor': '1', 'nome': 'Terça-feira'},
        {'valor': '2', 'nome': 'Quarta-feira'},
        {'valor': '3', 'nome': 'Quinta-feira'},
        {'valor': '4', 'nome': 'Sexta-feira'},
    ]

    if request.method == 'POST':
        dia_semana = request.POST.get('dia_semana')
        horario = request.POST.get('horario')
        turno = request.POST.get('turno')
        professor_id = request.POST.get('professor')
        disciplina = request.POST.get('disciplina')
        turma_id = request.POST.get('turma')
        observacao = request.POST.get('observacao', '')

        try:
            dias_offset = int(dia_semana)
        except (TypeError, ValueError):
            dias_offset = None

        if dias_offset not in range(5) or not cronograma_ativo_inicio:
            messages.error(request, 'Selecione um dia da semana valido para o agendamento.')
            from ..models import Professor, Turma
            professores = Professor.objects.filter(ativo=True)
            turmas = Turma.objects.filter(ativa=True)
            return render(request, 'laboratorios/agendar.html', {
                'laboratorio': laboratorio,
                'laboratorios': _laboratorios_agendaveis(),
                'professores': professores,
                'turmas': turmas,
                'cronograma_ativo_inicio': cronograma_ativo_inicio,
                'cronograma_ativo_fim': cronograma_ativo_fim,
                'dias_semana_agendamento': dias_semana_agendamento,
            })

        data = cronograma_ativo_inicio + timedelta(days=dias_offset)

        # 1. Verificar se o mesmo professor ja tem agendamento neste horario (em qualquer lab)
        conflito_professor = AgendamentoLab.objects.filter(
            professor_id=professor_id,
            data=data,
            horario=horario,
            turno=turno
        ).exists()

        if conflito_professor:
            messages.error(request, 'Este professor ja possui agendamento neste horario em outro laboratorio!')
            # Buscar dados para renderizar o formulario novamente
            from ..models import Professor, Turma
            professores = Professor.objects.filter(ativo=True)
            turmas = Turma.objects.filter(ativa=True)
            return render(request, 'laboratorios/agendar.html', {
                'laboratorio': laboratorio,
                'laboratorios': _laboratorios_agendaveis(),
                'professores': professores,
                'turmas': turmas,
                'cronograma_ativo_inicio': cronograma_ativo_inicio,
                'cronograma_ativo_fim': cronograma_ativo_fim,
                'dias_semana_agendamento': dias_semana_agendamento,
            })

        # 2. Verificar se o laboratorio ja esta reservado neste horario
        conflito_lab = AgendamentoLab.objects.filter(
            laboratorio=laboratorio,
            data=data,
            horario=horario,
            turno=turno
        ).exists()

        if conflito_lab:
            messages.error(request, f'{laboratorio.nome} ja esta reservado neste horario!')
            from ..models import Professor, Turma
            professores = Professor.objects.filter(ativo=True)
            turmas = Turma.objects.filter(ativa=True)
            return render(request, 'laboratorios/agendar.html', {
                'laboratorio': laboratorio,
                'laboratorios': _laboratorios_agendaveis(),
                'professores': professores,
                'turmas': turmas,
                'cronograma_ativo_inicio': cronograma_ativo_inicio,
                'cronograma_ativo_fim': cronograma_ativo_fim,
                'dias_semana_agendamento': dias_semana_agendamento,
            })

        # 3. Salvar agendamento
        from ..models import Professor, Turma
        AgendamentoLab.objects.create(
            laboratorio=laboratorio,
            data=data,
            horario=horario,
            turno=turno,
            professor_id=professor_id,
            disciplina=disciplina,
            turma_id=turma_id,
            observacao=observacao,
            registrado_por=request.user
        )
        messages.success(request, f'{laboratorio.nome} agendado com sucesso!')
        return redirect('listar_laboratorios')

    # GET - mostrar formulario
    from ..models import Professor, Turma
    professores = Professor.objects.filter(ativo=True)
    turmas = Turma.objects.filter(ativa=True)

    return render(request, 'laboratorios/agendar.html', {
        'laboratorio': laboratorio,
        'laboratorios': _laboratorios_agendaveis(),
        'professores': professores,
        'turmas': turmas,
        'cronograma_ativo_inicio': cronograma_ativo_inicio,
        'cronograma_ativo_fim': cronograma_ativo_fim,
        'dias_semana_agendamento': dias_semana_agendamento,
    })


@login_required
@user_passes_test(pode_ver_cronograma_laboratorios, login_url='/')
def cronograma_semanal(request, turno_forcado=None):
    """Exibe o cronograma semanal dos laboratorios, com filtro opcional por turno.

    Perfil somente leitura (grupo "Consulta Cronograma", ou Equipe Diretiva/
    superuser): a checagem de acesso acima (pode_ver_cronograma_laboratorios)
    cobre GET e POST igualmente, mas escrita exige uma checagem adicional e
    mais restrita (pertence_ao_grupo_equipe_diretiva) logo no inicio do bloco
    POST abaixo - um membro so de "Consulta Cronograma" nunca passa dali,
    mesmo montando um POST valido para uma semana ativa/editando.

    `turno_forcado` chega apenas via a rota travada por turno (URLs
    /laboratorios/cronograma/manha/ e /laboratorios/cronograma/tarde/, ver
    apps/urls.py). Quando presente, tem prioridade absoluta sobre qualquer
    valor de turno vindo da querystring ou do corpo do POST: a tela so
    trabalha com aquele turno do inicio ao fim, e nao ha como um GET/POST
    manipulado manualmente (ex.: ?turno=tarde numa URL travada em manha, ou
    um campo 'turno' forjado no POST) fazer a view ler ou escrever dados do
    outro turno.
    """
    # A URL sem turno existe apenas para compatibilidade com links antigos.
    # Ela nunca renderiza uma grade nem processa escrita: bookmarks com turno
    # seguem para a URL travada correspondente; sem turno, cada perfil volta
    # para sua pagina de entrada apropriada.
    if turno_forcado is None:
        if request.method != 'GET':
            messages.error(request, 'A rota generica do cronograma nao aceita alteracoes.')
            if pertence_ao_grupo_equipe_diretiva(request.user):
                return redirect('diagnostico_cronograma_laboratorios')
            return redirect('consulta_cronograma_laboratorios')

        turno_compatibilidade = request.GET.get('turno', '')
        if turno_compatibilidade in ('manha', 'tarde'):
            return redirect(_url_cronograma_com_filtros(
                request.GET.get('data_inicio', ''),
                turno_compatibilidade,
                request.GET.get('laboratorio', ''),
                request.GET.get('scroll_y', ''),
                turno_forcado=turno_compatibilidade,
            ))

        if pertence_ao_grupo_equipe_diretiva(request.user):
            return redirect('diagnostico_cronograma_laboratorios')
        return redirect('consulta_cronograma_laboratorios')

    # Capturar o filtro de turno vindo da URL travada.
    turno_filtro = turno_forcado if turno_forcado else request.GET.get('turno', '')
    laboratorio_filtro = request.GET.get('laboratorio', '')
    modo_edicao = bool(request.GET.get('data_inicio')) or bool(turno_forcado)
    data_inicio, data_fim = _periodo_cronograma(request, turno_forcado=turno_forcado)
    ativo_inicio, ativo_fim = obter_cronograma_ativo(turno_filtro)
    cronograma_exibido_ativo = data_inicio == ativo_inicio and data_fim == ativo_fim

    if request.method == 'POST':
        # Perfil somente leitura (ex.: grupo "Consulta Cronograma"): bloqueado
        # aqui, antes de qualquer outra logica, mesmo que a semana pedida
        # esteja ativa/editando e o restante do POST seja perfeitamente
        # valido. Escrita exige pertencer a Equipe Diretiva (ou superuser) -
        # pode_ver_cronograma_laboratorios (checado no decorator da view,
        # acima) e deliberadamente mais permissivo e nao basta para escrever.
        if not pertence_ao_grupo_equipe_diretiva(request.user):
            messages.error(request, 'Você não tem permissão para alterar o cronograma.')
            return redirect('diagnostico_cronograma_laboratorios')

        agendamento_id = request.POST.get('agendamento_id', '').strip()
        laboratorio_id = request.POST.get('laboratorio_id')
        data_agendamento = request.POST.get('data')
        horario = request.POST.get('horario')
        # Na rota travada por turno, o turno nunca vem do POST - e sempre o
        # da URL, mesmo que o formulario (ou uma requisicao forjada) envie
        # outro valor no campo 'turno'.
        turno = turno_forcado if turno_forcado else request.POST.get('turno')
        professor_id = request.POST.get('professor')
        turma_id = request.POST.get('turma')
        disciplina = request.POST.get('disciplina', '').strip()
        observacao = request.POST.get('observacao', '').strip()
        data_inicio_retorno = request.POST.get('data_inicio', '')
        turno_retorno = request.POST.get('turno_filtro', '')
        laboratorio_retorno = request.POST.get('laboratorio_filtro', '')
        scroll_y = request.POST.get('scroll_y', '')
        url_retorno = _url_cronograma_com_filtros(
            data_inicio_retorno, turno_retorno, laboratorio_retorno, scroll_y,
            turno_forcado=turno_forcado,
        )

        try:
            data_obj = datetime.strptime(data_agendamento, '%Y-%m-%d').date()
        except (TypeError, ValueError):
            messages.error(request, 'Data invalida para o agendamento.')
            return redirect(url_retorno)

        # Fase 2 (secoes 11 e 14): protecao de escrita centralizada e
        # obrigatoria, consultando SemanaCronogramaLaboratorio - aplicada da
        # MESMA forma para as duas rotas travadas por turno E para a rota
        # generica (turno_forcado=None). So escreve (cria, edita ou
        # reaproveita um AgendamentoLab) quando a semana correspondente ao
        # turno+data existir e estiver ATIVA ou EM EDICAO. Bloqueia semana
        # historica, cancelada, inexistente, turno invalido, ou qualquer
        # tentativa de escrever via URL/POST manipulado manualmente. GET
        # continua liberado para consulta em qualquer data (nao passa por
        # aqui). Para turnos que ainda nao adotaram a tabela nova, cai no
        # comportamento legado (Fase 1 e anteriores), intacto.
        semana_destino = None
        bloqueio_escrita = None

        if turno not in ('manha', 'tarde'):
            bloqueio_escrita = 'Turno invalido para o agendamento.'
        elif _turno_usa_semana_nova(turno):
            try:
                # Identidade explicita da versao exibida; nunca e inferida
                # pela segunda-feira correspondente ao dia do agendamento.
                versao_inicio = datetime.strptime(data_inicio_retorno, '%Y-%m-%d').date()
            except (TypeError, ValueError):
                versao_inicio = None

            semana_destino = _semana_por_turno_e_data(turno, versao_inicio)
            if versao_inicio is None or semana_destino is None:
                bloqueio_escrita = (
                    'Nao existe nenhuma semana cadastrada para este turno e data '
                    '(nem ativa, nem em edicao). Prepare a semana antes de alterar o cronograma.'
                )
            elif not (semana_destino.data_inicio <= data_obj <= semana_destino.data_fim):
                bloqueio_escrita = (
                    'A data do agendamento nao pertence ao intervalo desta versao do cronograma.'
                )
            elif semana_destino.status == 'historica':
                bloqueio_escrita = 'Esta semana e historica e nao pode receber novos agendamentos ou alteracoes.'
            elif semana_destino.status == 'cancelada':
                bloqueio_escrita = 'Esta semana foi cancelada e nao pode receber novos agendamentos ou alteracoes.'
            elif not semana_destino.pode_receber_escrita:
                bloqueio_escrita = 'Esta semana nao esta disponivel para escrita.'
        else:
            # Fallback legado: sem SemanaCronogramaLaboratorio, a versao era
            # inferida pela semana civil que contem a data do agendamento.
            semana_solicitada = _inicio_semana(data_obj)
            ativo_inicio_turno, _ativo_fim_turno = obter_cronograma_ativo(turno)
            if semana_solicitada not in (ativo_inicio_turno, _semana_editando(turno)):
                bloqueio_escrita = (
                    'Esta semana não está ativa nem marcada como em edição. '
                    'Prepare a semana antes de alterar o cronograma.'
                )

        if bloqueio_escrita:
            messages.error(request, bloqueio_escrita)
            if turno_forcado:
                return redirect('diagnostico_cronograma_laboratorios')
            return redirect(url_retorno)

        agendamento = None
        if agendamento_id:
            agendamento = get_object_or_404(AgendamentoLab, id=agendamento_id)
            if turno_forcado and agendamento.turno != turno_forcado:
                # Defesa em profundidade: mesmo que alguem monte manualmente um
                # POST com o id de um agendamento do outro turno, a tela travada
                # em manha (ou tarde) se recusa a ler ou alterar esse registro.
                messages.error(
                    request,
                    'Este agendamento pertence ao outro turno e nao pode ser alterado por esta tela.'
                )
                return redirect(url_retorno)
            laboratorio = agendamento.laboratorio
        else:
            laboratorio = get_object_or_404(Laboratorio, id=laboratorio_id, ativo=True)

        # Fase 2 (correcao do bug de conflito): quando o turno ja adotou
        # SemanaCronogramaLaboratorio, a checagem de conflito e restrita a
        # versao aberta (semana_destino) - um agendamento de outra versao
        # (ativa, editando, historica ou cancelada) do MESMO turno/data/
        # horario nao pode bloquear o cadastro na versao atual, ja que cada
        # versao e um cronograma independente. Turnos legados (sem a tabela
        # nova) preservam o comportamento antigo, filtrando so por
        # turno+data+horario.
        if semana_destino is not None:
            conflito_professor = AgendamentoLab.objects.filter(
                professor_id=professor_id,
                data=data_obj,
                horario=horario,
                turno=turno,
                semana=semana_destino,
            )
            conflito_lab = AgendamentoLab.objects.filter(
                laboratorio=laboratorio,
                data=data_obj,
                horario=horario,
                turno=turno,
                semana=semana_destino,
            )
        else:
            conflito_professor = AgendamentoLab.objects.filter(
                professor_id=professor_id,
                data=data_obj,
                horario=horario,
                turno=turno,
            )
            conflito_lab = AgendamentoLab.objects.filter(
                laboratorio=laboratorio,
                data=data_obj,
                horario=horario,
                turno=turno,
            )
        if agendamento:
            conflito_professor = conflito_professor.exclude(id=agendamento.id)
            conflito_lab = conflito_lab.exclude(id=agendamento.id)

        if conflito_professor.exists():
            messages.error(request, 'Este professor ja possui agendamento neste horario em outro laboratorio!')
            return redirect(url_retorno)

        if conflito_lab.exists():
            messages.error(request, f'{laboratorio.nome} ja esta reservado neste horario!')
            return redirect(url_retorno)

        if agendamento is None:
            agendamento = AgendamentoLab(laboratorio=laboratorio, registrado_por=request.user)

        agendamento.data = data_obj
        agendamento.horario = horario
        agendamento.turno = turno
        agendamento.professor_id = professor_id
        agendamento.turma_id = turma_id
        agendamento.disciplina = disciplina
        agendamento.observacao = observacao
        # Fase 2 (secao 12): todo AgendamentoLab criado/editado pela grade,
        # num turno que ja adotou a tabela nova, recebe obrigatoriamente a
        # semana correspondente (recalculada a partir da data/turno que estao
        # sendo salvos agora - nunca reaproveita ciegamente uma FK antiga).
        # Em turno ainda nao adotado (legado), fica sem semana, como sempre foi.
        if semana_destino is not None:
            agendamento.semana = semana_destino
        try:
            with transaction.atomic():
                agendamento.save()
        except IntegrityError:
            # unique_together de AgendamentoLab e (laboratorio, data, horario,
            # turno) - NAO inclui semana. Duas versoes do mesmo turno com
            # janelas sobrepostas (ver _versao_ativa_turno) podem ter o mesmo
            # laboratorio+data+horario+turno cada uma na sua propria versao;
            # o filtro de conflito acima ja isola por semana_destino e deixa
            # passar esse caso, mas o banco ainda bloqueia no nivel de linha.
            # Sem migration para adicionar semana a essa constraint, so resta
            # devolver a mesma mensagem amigavel em vez de deixar o
            # IntegrityError virar erro 500.
            messages.error(request, f'{laboratorio.nome} ja esta reservado neste horario!')
            return redirect(url_retorno)

        messages.success(request, 'Agendamento salvo com sucesso!')
        return redirect(url_retorno)

    semana_exibida_obj = None
    if modo_edicao and turno_filtro in ('manha', 'tarde'):
        # Fase 2: o estado exibido vem de _estado_semana_para_exibicao, que
        # consulta SemanaCronogramaLaboratorio quando o turno ja a adotou
        # (suportando tambem historica/cancelada, que nao existiam antes),
        # ou cai na regra antiga (ativo_inicio/_semana_editando) para turnos
        # ainda nao adotados - nunca olha para o outro turno.
        turno_label = 'Manhã' if turno_filtro == 'manha' else 'Tarde'
        estado_exibido, semana_exibida_obj = _estado_semana_para_exibicao(
            turno_filtro, data_inicio, ativo_inicio,
        )
        if estado_exibido == 'ativa':
            cronograma_titulo = (
                f'🟢 Cronograma Ativo — {turno_label} · Início: '
                f'{data_inicio.strftime("%d/%m/%Y")}'
            )
        elif estado_exibido == 'editando':
            cronograma_titulo = (
                f'📝 Editando Cronograma — {turno_label} · Início: '
                f'{data_inicio.strftime("%d/%m/%Y")}'
            )
        elif estado_exibido == 'historica':
            cronograma_titulo = (
                f'📁 Semana Histórica — {turno_label} · Início: '
                f'{data_inicio.strftime("%d/%m/%Y")}'
            )
        elif estado_exibido == 'cancelada':
            cronograma_titulo = (
                f'🚫 Semana Cancelada — {turno_label} · Início: '
                f'{data_inicio.strftime("%d/%m/%Y")}'
            )
        else:
            cronograma_titulo = (
                f'🔍 Visualizando Semana — {turno_label} · Início: '
                f'{data_inicio.strftime("%d/%m/%Y")}'
            )
    elif modo_edicao:
        cronograma_titulo = f'✏️ Cronograma em edição — {"Ativo" if cronograma_exibido_ativo else "Não ativo"}'
    else:
        cronograma_titulo = '📅 Cronograma ativo'

    laboratorios_todos = _laboratorios_agendaveis()
    laboratorios = _laboratorios_filtrados(laboratorio_filtro)

    agendamentos = AgendamentoLab.objects.filter(
        data__gte=data_inicio,
        data__lte=data_fim,
        laboratorio__in=laboratorios,
    )

    if semana_exibida_obj is not None:
        # Janelas de versoes com datas de inicio distintas podem se sobrepor
        # (por exemplo, 17/08 e 23/08). A FK preserva qual versao esta aberta
        # e impede que apenas o intervalo de datas misture seus agendamentos.
        agendamentos = agendamentos.filter(semana=semana_exibida_obj)

    # Aplicar filtro de turno se fornecido
    if turno_filtro in ['manha', 'tarde']:
        agendamentos = agendamentos.filter(turno=turno_filtro)

    agendamentos = agendamentos.select_related('laboratorio', 'professor', 'turma').order_by(
        'laboratorio__nome', 'data', 'horario'
    )
    agendamentos_lista = list(agendamentos)
    agendamentos_cards = _agendamentos_cards(agendamentos_lista)
    agendamentos_dict = _agendamentos_por_chave(agendamentos_lista)
    horarios_por_lab = {}
    for agendamento in agendamentos_lista:
        if not _laboratorio_considerado_cronograma(agendamento.laboratorio):
            continue
        horarios_por_lab.setdefault(agendamento.laboratorio, set()).add(str(agendamento.horario))
    laboratorios_disponiveis = [
        {
            'laboratorio': laboratorio,
            'aulas': [
                f'{horario}ª aula'
                for horario in sorted(horarios, key=lambda valor: int(valor) if valor.isdigit() else 999)
            ],
        }
        for laboratorio, horarios in sorted(horarios_por_lab.items(), key=lambda item: item[0].nome)
    ]
    dias_cronograma = _dias_cronograma(data_inicio)
    grupos_turno_cronograma = _grupos_turno_cronograma(turno_filtro, agendamentos_cards)
    cronograma_grupos = []
    for grupo in grupos_turno_cronograma:
        grupo_cronograma = dict(grupo)
        grupo_cronograma['laboratorios'] = []
        for lab in laboratorios:
            linhas = []
            for slot in grupo['slots']:
                celulas = []
                for dia in dias_cronograma:
                    agendamento_celula = agendamentos_dict.get((lab.id, dia['nome'], slot['turno'], slot['horario']))
                    celulas.append({
                        'dia': dia,
                        'agendamento': agendamento_celula,
                    })
                linhas.append({
                    'slot': slot,
                    'celulas': celulas,
                })
            grupo_cronograma['laboratorios'].append({
                'laboratorio': lab,
                'linhas': linhas,
            })
        cronograma_grupos.append(grupo_cronograma)

    # Preferencia de interface (nao e a protecao real, que ja esta no POST
    # acima, e que tambem passou a valer para a rota generica na Fase 2): na
    # rota travada por turno, so oferece os controles de criar/editar da
    # grade quando a semana exibida esta ativa ou em edicao (historica e
    # cancelada mostram a grade em modo somente-leitura). Na rota generica
    # (turno_forcado=None) a interface continua liberada como antes, sujeita
    # sempre a mesma condicao abaixo. Em qualquer caso, um usuario que nao
    # pertence a Equipe Diretiva (ex.: grupo "Consulta Cronograma") nunca ve
    # nenhum controle de escrita, independente do status da semana - essa e
    # a mesma checagem que o POST usa de verdade, entao a interface nunca
    # promete algo que o backend recusaria.
    usuario_pode_escrever = pertence_ao_grupo_equipe_diretiva(request.user)
    if turno_forcado:
        estado_exibido_grade, _semana_exibida_grade_obj = _estado_semana_para_exibicao(
            turno_forcado, data_inicio, ativo_inicio,
        )
        pode_editar_grade = usuario_pode_escrever and estado_exibido_grade in ('ativa', 'editando')
    else:
        pode_editar_grade = usuario_pode_escrever

    context = {
        'dias_semana': DIAS_SEMANA_LAB,
        'dias_cronograma': dias_cronograma,
        'horarios': HORARIOS_LAB,
        'slots_cronograma': _slots_cronograma(turno_filtro),
        'agendamentos_dict': agendamentos_dict,
        'agendamentos_cards': agendamentos_cards,
        'grupos_turno_cronograma': grupos_turno_cronograma,
        'cronograma_grupos': cronograma_grupos,
        'laboratorios': laboratorios,
        'laboratorios_todos': laboratorios_todos,
        'laboratorios_disponiveis': laboratorios_disponiveis,
        'professores': Professor.objects.filter(ativo=True).order_by('nome_completo'),
        'turmas': Turma.objects.filter(ativa=True).order_by('nome'),
        'data_inicio': data_inicio,
        'data_fim': data_fim,
        'cronograma_titulo': cronograma_titulo,
        'turno_filtro': turno_filtro,
        'laboratorio_filtro': laboratorio_filtro,
        'turno_bloqueado': turno_forcado,
        'pode_editar_grade': pode_editar_grade,
    }
    return render(request, 'laboratorios/cronograma.html', context)

@login_required
@user_passes_test(pode_ver_cronograma_laboratorios, login_url='/')
def cronograma_print(request):
    """Exibe o cronograma semanal para impressao (somente leitura - liberado
    tambem para o grupo "Consulta Cronograma")."""
    # 🔥 CAPTURAR TURNO (FALTAVA ISSO)
    turno_filtro = request.GET.get('turno', '')
    laboratorio_filtro = request.GET.get('laboratorio', '')
    data_inicio, data_fim = _periodo_cronograma(request)

    laboratorios_todos = _laboratorios_agendaveis()
    laboratorios = _laboratorios_filtrados(laboratorio_filtro)

    # 📊 Buscar agendamentos
    agendamentos = AgendamentoLab.objects.filter(
        data__gte=data_inicio,
        data__lte=data_fim,
        laboratorio__in=laboratorios,
    )

    # 🔥 APLICAR FILTRO (FALTAVA ISSO)
    if turno_filtro in ['manha', 'tarde']:
        agendamentos = agendamentos.filter(turno=turno_filtro)

    agendamentos = agendamentos.select_related('laboratorio', 'professor', 'turma').order_by(
        'laboratorio__nome', 'data', 'horario'
    )
    agendamentos_lista = list(agendamentos)
    agendamentos_cards = _agendamentos_cards(agendamentos_lista)

    # 📤 Contexto
    context = {
        'dias_semana': DIAS_SEMANA_LAB,
        'horarios': HORARIOS_LAB,
        'slots_cronograma': _slots_cronograma(turno_filtro),
        'agendamentos_dict': _agendamentos_por_chave(agendamentos_lista),
        'agendamentos_cards': agendamentos_cards,
        'grupos_turno_cronograma': _grupos_turno_cronograma(turno_filtro, agendamentos_cards),
        'laboratorios': laboratorios,
        'laboratorios_todos': laboratorios_todos,
        'data_inicio': data_inicio,
        'data_fim': data_fim,
        'turno_filtro': turno_filtro,
        'laboratorio_filtro': laboratorio_filtro,
    }

    return render(request, 'laboratorios/cronograma_print.html', context)

    import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill
from django.http import HttpResponse

@login_required
@user_passes_test(pode_ver_cronograma_laboratorios, login_url='/')
def exportar_cronograma_excel(request):
    """Exporta o cronograma semanal para Excel (somente leitura - liberado
    tambem para o grupo "Consulta Cronograma")."""
    from ..models import Laboratorio, AgendamentoLab
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter

    # Captura o filtro de turno (vem do template)
    turno_filtro = request.GET.get('turno', '')
    laboratorio_filtro = request.GET.get('laboratorio', '')
    data_inicio, data_fim = _periodo_cronograma(request)

    laboratorios = _laboratorios_filtrados(laboratorio_filtro)
    dias_semana = DIAS_SEMANA_LAB
    horarios = HORARIOS_LAB

    # Buscar agendamentos da semana (com filtro de turno)
    agendamentos = AgendamentoLab.objects.filter(
        data__gte=data_inicio,
        data__lte=data_fim,
        laboratorio__in=laboratorios,
    )
    if turno_filtro in ['manha', 'tarde']:
        agendamentos = agendamentos.filter(turno=turno_filtro)
    agendamentos = agendamentos.select_related('laboratorio', 'professor', 'turma')

    # Dicionario para acesso rapido
    agendamentos_dict = _agendamentos_por_chave(agendamentos)

    wb = Workbook()
    wb.remove(wb.active)

    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )

    for lab in laboratorios:
        ws = wb.create_sheet(title=lab.nome)

        # Cabecalho: dias da semana (uma coluna por dia)
        for idx, dia in enumerate(dias_semana, start=2):
            cell = ws.cell(row=1, column=idx, value=dia)
            cell.font = Font(bold=True, size=11)
            cell.alignment = Alignment(horizontal='center', vertical='center')
            cell.fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
            cell.border = thin_border

        # Agora, para cada horario, mesclar 3 linhas na coluna A e preencher as outras colunas
        row_offset = 2  # comeca na linha 2 (apos cabecalho)
        for horario_idx, horario in enumerate(horarios):
            base_row = row_offset + horario_idx * 3

            # Mescla as 3 linhas na coluna A (horario)
            ws.merge_cells(start_row=base_row, start_column=1, end_row=base_row+2, end_column=1)
            cell_horario = ws.cell(row=base_row, column=1, value=f"Aula{horario}")
            cell_horario.font = Font(bold=True, size=11)
            cell_horario.alignment = Alignment(horizontal='center', vertical='center')
            cell_horario.border = thin_border

            # Para cada dia da semana
            for col_idx, dia in enumerate(dias_semana, start=2):
                key = (lab.id, dia, 'manha' if turno_filtro == 'manha' else 'tarde' if turno_filtro == 'tarde' else 'manha', horario)
                a = agendamentos_dict.get(key)
                if not a and turno_filtro not in ['manha', 'tarde']:
                    a = agendamentos_dict.get((lab.id, dia, 'tarde', horario))

                # Linha da disciplina
                ws.cell(row=base_row, column=col_idx, value=a.disciplina if a else "").border = thin_border
                ws.cell(row=base_row, column=col_idx).alignment = Alignment(horizontal='center', vertical='center')
                # Linha da turma
                ws.cell(row=base_row+1, column=col_idx, value=a.turma.nome if a else "").border = thin_border
                ws.cell(row=base_row+1, column=col_idx).alignment = Alignment(horizontal='center', vertical='center')
                # Linha do professor
                ws.cell(row=base_row + 2, column=col_idx,
                        value=a.professor.nome_abreviado if a else "").border = thin_border
                ws.cell(row=base_row+2, column=col_idx).alignment = Alignment(horizontal='center', vertical='center')

        # Ajustar larguras
        ws.column_dimensions['A'].width = 12
        for col in range(2, 2 + len(dias_semana)):
            col_letter = get_column_letter(col)
            ws.column_dimensions[col_letter].width = 28

        # Ajustar altura das linhas (cada bloco de 3 linhas = 60, cada linha 20)
        for row in range(row_offset, row_offset + len(horarios)*3):
            ws.row_dimensions[row].height = 20

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="cronograma_{data_inicio.strftime("%Y%m%d")}.xlsx"'
    wb.save(response)
    return response


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def emprestimo_equipamento(request, lab_id):
    """Registrar emprestimo de equipamentos (Lab 04 ou Lab 05)"""
    laboratorio = get_object_or_404(Laboratorio, id=lab_id, tipo='itinerante')

    # Equipamentos disponiveis
    equipamentos_disponiveis = ItemEquipamento.objects.filter(
        laboratorio=laboratorio,
        disponivel=True
    )

    if request.method == 'POST':
        tipo_emprestimo = request.POST.get('tipo_emprestimo')
        quantidade = int(request.POST.get('quantidade', 0))
        data_prevista = request.POST.get('data_prevista_devolucao')
        motivo = request.POST.get('motivo')
        aulas_utilizacao = request.POST.get('aulas_utilizacao', '')
        observacao = request.POST.get('observacao', '')

        # Pegar equipamentos selecionados (checkbox)
        itens_selecionados = request.POST.getlist('itens')

        # Validar
        if len(itens_selecionados) != quantidade:
            messages.error(request, 'Selecione a quantidade correta de equipamentos!')
        else:
            # Criar emprestimo
            emprestimo = Emprestimo.objects.create(
                tipo_emprestimo=tipo_emprestimo,
                laboratorio=laboratorio,
                quantidade=quantidade,
                data_prevista_devolucao=data_prevista,
                motivo=motivo,
                aulas_utilizacao=aulas_utilizacao,
                observacao=observacao,
                registrado_por=request.user
            )

            # Adicionar itens e marcar como indisponiveis
            for item_id in itens_selecionados:
                item = ItemEquipamento.objects.get(id=item_id)
                emprestimo.itens.add(item)
                item.disponivel = False
                item.save()

            # Se for professor
            if tipo_emprestimo == 'professor':
                emprestimo.professor_id = request.POST.get('professor_id')
            elif tipo_emprestimo == 'aluno':
                emprestimo.aluno_id = request.POST.get('aluno_id')
            elif tipo_emprestimo == 'turma':
                emprestimo.turma_id = request.POST.get('turma_id')

            emprestimo.save()
            messages.success(request, f'Emprestimo registrado! {quantidade} equipamento(s) emprestado(s).')
            return redirect('listar_laboratorios')

    # GET - mostrar formulario
    from ..models import Professor, Aluno, Turma
    professores = Professor.objects.filter(ativo=True)
    alunos = Aluno.objects.filter(ativo=True)
    turmas = Turma.objects.filter(ativa=True)

    return render(request, 'laboratorios/emprestimo.html', {
        'laboratorio': laboratorio,
        'equipamentos': equipamentos_disponiveis,
        'professores': professores,
        'alunos': alunos,
        'turmas': turmas,
    })


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def devolver_equipamento(request, emprestimo_id):
    """Registrar devolucao de equipamentos"""
    emprestimo = get_object_or_404(Emprestimo, id=emprestimo_id)

    if request.method == 'POST':
        from django.utils import timezone
        emprestimo.data_devolucao = timezone.now()
        emprestimo.status = 'devolvido'
        emprestimo.save()

        # Marcar equipamentos como disponiveis novamente
        for item in emprestimo.itens.all():
            item.disponivel = True
            item.save()

        messages.success(request, 'Equipamentos devolvidos com sucesso!')
        return redirect('listar_emprestimos')

    return render(request, 'laboratorios/devolver.html', {'emprestimo': emprestimo})


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def listar_emprestimos(request):
    """Lista todos os emprestimos ativos"""
    emprestimos = Emprestimo.objects.filter(status='emprestado').order_by('data_prevista_devolucao')
    return render(request, 'laboratorios/lista_emprestimos.html', {'emprestimos': emprestimos})
