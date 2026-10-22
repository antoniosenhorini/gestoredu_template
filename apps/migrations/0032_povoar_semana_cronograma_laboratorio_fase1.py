# Migration de dados - Fase 1 da reestrutura de semanas do cronograma de
# laboratorios.
#
# Le o estado ATUAL (no momento em que esta migration realmente roda, nao no
# momento em que foi escrita) de ConfiguracaoCronogramaLaboratorio e cria os
# registros correspondentes em SemanaCronogramaLaboratorio (tabela nova,
# criada na migration anterior). Depois, vincula os AgendamentoLab
# existentes a semana correspondente quando a associacao for inequivoca.
#
# Importante: esta migration NAO altera nenhum campo de
# ConfiguracaoCronogramaLaboratorio (nem os novos, nem os legados) - so
# leitura. Tambem nao apaga nem altera nenhum campo funcional de
# AgendamentoLab alem de preencher a nova FK "semana" quando a associacao
# for inequivoca.
#
# ativado_em / criado_em das semanas "ativa"/"editando" criadas aqui usam
# timezone.now() puro (corte inaugural), e nao uma tentativa de reconstruir
# o instante real em que a semana foi ativada/criada a partir de
# atualizado_em da config - isso seria impreciso, pois atualizado_em reflete
# a ultima escrita na config por qualquer motivo, nao necessariamente a
# ativacao/criacao daquela semana especifica.
from datetime import timedelta

from django.db import migrations
from django.utils import timezone


def criar_semanas_e_vincular_agendamentos(apps, schema_editor):
    ConfiguracaoCronogramaLaboratorio = apps.get_model('apps', 'ConfiguracaoCronogramaLaboratorio')
    SemanaCronogramaLaboratorio = apps.get_model('apps', 'SemanaCronogramaLaboratorio')
    AgendamentoLab = apps.get_model('apps', 'AgendamentoLab')

    # So leitura da config - nunca escrita. Em condicoes normais existe
    # exatamente 1 linha; se por alguma razao existir mais de uma no momento
    # real da execucao (nao deveria - fora do escopo desta migration
    # investigar/corrigir isso), usamos defensivamente a mais recente por
    # atualizado_em para nao quebrar a migration, mas isso e um sintoma de
    # anomalia de dados que precisa ser investigado separadamente.
    config = ConfiguracaoCronogramaLaboratorio.objects.order_by('-atualizado_em').first()
    if config is None:
        # Nao ha config alguma - nada a fazer nesta instalacao.
        return

    agora = timezone.now()

    # (objeto_semana, turno, data_inicio, data_fim) de cada semana criada
    # nesta execucao, para depois vincular os AgendamentoLab.
    semanas_criadas = []

    for turno in ('manha', 'tarde'):
        data_inicio_ativo = getattr(config, f'data_inicio_{turno}_ativo')
        if data_inicio_ativo:
            semana_ativa = SemanaCronogramaLaboratorio.objects.create(
                turno=turno,
                data_inicio=data_inicio_ativo,
                status='ativa',
                criado_em=agora,
                ativado_em=agora,
            )
            semanas_criadas.append(
                (semana_ativa, turno, data_inicio_ativo, data_inicio_ativo + timedelta(days=6))
            )

        data_inicio_editando = getattr(config, f'data_inicio_{turno}_editando')
        if data_inicio_editando:
            semana_editando = SemanaCronogramaLaboratorio.objects.create(
                turno=turno,
                data_inicio=data_inicio_editando,
                status='editando',
                criado_em=agora,
            )
            semanas_criadas.append(
                (semana_editando, turno, data_inicio_editando, data_inicio_editando + timedelta(days=6))
            )

    # Vincula AgendamentoLab existentes as semanas recem-criadas, somente
    # quando a correspondencia (turno + data dentro de [inicio, inicio+6])
    # for inequivoca, isto e, exatamente uma semana recem-criada cobre
    # aquele turno+data. Agendamentos fora de qualquer semana conhecida (ex.
    # semanas historicas antigas, que nao tem SemanaCronogramaLaboratorio
    # correspondente nesta fase) ou que caiam ambiguamente em mais de uma
    # semana (nao deveria ocorrer nesta migracao, pois so ha no maximo 1
    # ativa + 1 editando por turno hoje) permanecem com semana=NULL - nada
    # e inventado nem apagado.
    for turno in ('manha', 'tarde'):
        semanas_do_turno = [s for s in semanas_criadas if s[1] == turno]
        if not semanas_do_turno:
            continue
        agendamentos = AgendamentoLab.objects.filter(turno=turno, semana__isnull=True)
        for agendamento in agendamentos:
            candidatas = [
                semana for (semana, _turno, ini, fim) in semanas_do_turno
                if ini <= agendamento.data <= fim
            ]
            if len(candidatas) == 1:
                agendamento.semana = candidatas[0]
                agendamento.save(update_fields=['semana'])
            # 0 candidatas -> fora de qualquer semana conhecida, fica NULL.
            # >1 candidatas -> ambiguo, fica NULL (nao deveria acontecer).


def reverter(apps, schema_editor):
    # No momento em que esta migration roda (fase 1), a tabela
    # SemanaCronogramaLaboratorio acabou de ser criada pela migration
    # anterior e nenhum outro fluxo do sistema escreve nela ainda - logo,
    # todo o conteudo presente nela em qualquer momento antes de uma fase 2
    # futura habilitar outros escritores e, por construcao, exatamente o que
    # esta migration criou. Por isso apagar "todas as linhas da tabela" aqui
    # e equivalente a apagar "so as linhas criadas por esta migration".
    SemanaCronogramaLaboratorio = apps.get_model('apps', 'SemanaCronogramaLaboratorio')
    AgendamentoLab = apps.get_model('apps', 'AgendamentoLab')

    AgendamentoLab.objects.filter(semana__isnull=False).update(semana=None)
    SemanaCronogramaLaboratorio.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('apps', '0031_semana_cronograma_laboratorio_fase1'),
    ]

    operations = [
        migrations.RunPython(criar_semanas_e_vincular_agendamentos, reverter),
    ]
