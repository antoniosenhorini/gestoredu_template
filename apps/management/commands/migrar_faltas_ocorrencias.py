from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from apps.models import RegistroFaltaAluno, RegistroOcorrenciaAluno


TIPOS_FALTA_EM_OCORRENCIAS = ('falta', 'Falta')


class Command(BaseCommand):
    help = 'Migra ocorrencias antigas do tipo falta para RegistroFaltaAluno sem alterar ou apagar registros antigos.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--confirmar',
            action='store_true',
            help='Grava os registros em RegistroFaltaAluno. Sem esta opcao, executa apenas dry-run.',
        )

    def handle(self, *args, **options):
        confirmar = options['confirmar']
        ocorrencias = (
            RegistroOcorrenciaAluno.objects.filter(tipo_ocorrencia__in=TIPOS_FALTA_EM_OCORRENCIAS)
            .select_related('aluno', 'registrado_por')
            .order_by('data', 'aluno_id', 'id')
        )

        total_encontrado = ocorrencias.count()
        total_migrado = 0
        duplicados_ignorados = 0

        existentes = set(
            RegistroFaltaAluno.objects.filter(
                aluno_id__in=ocorrencias.values_list('aluno_id', flat=True),
                data__in=ocorrencias.values_list('data', flat=True),
            ).values_list('aluno_id', 'data')
        )
        vistos_nesta_execucao = set()
        faltas_para_criar = []

        for ocorrencia in ocorrencias:
            chave = (ocorrencia.aluno_id, ocorrencia.data)
            if chave in existentes or chave in vistos_nesta_execucao:
                duplicados_ignorados += 1
                continue

            vistos_nesta_execucao.add(chave)
            observacoes = self._montar_observacoes(ocorrencia)
            faltas_para_criar.append({
                'aluno_id': ocorrencia.aluno_id,
                'data': ocorrencia.data,
                'quantidade_faltas': 1,
                'justificada': False,
                'responsavel_contatado': ocorrencia.responsavel_contatado or '',
                'observacoes': observacoes,
                'pedagoga': ocorrencia.atendido_por or '',
                'registrado_por_id': ocorrencia.registrado_por_id,
                'registrado_em': timezone.now(),
            })

        total_sera_migrado = len(faltas_para_criar)

        self.stdout.write('Migracao de faltas registradas como ocorrencias')
        self.stdout.write(f'Modo: {"CONFIRMACAO" if confirmar else "DRY-RUN"}')
        self.stdout.write(f'Total encontrado: {total_encontrado}')
        self.stdout.write(f'Total que sera migrado: {total_sera_migrado}')
        self.stdout.write(f'Duplicados ignorados: {duplicados_ignorados}')

        if confirmar and faltas_para_criar:
            with transaction.atomic():
                self._inserir_faltas(faltas_para_criar)
            total_migrado = total_sera_migrado

        if confirmar:
            self.stdout.write(f'Total migrado: {total_migrado}')
        else:
            self.stdout.write('Nenhum registro foi gravado. Use --confirmar para migrar de verdade.')

        self.stdout.write('Registros antigos nao foram removidos.')
        self.stdout.write(f'Pendentes em RegistroOcorrenciaAluno: {total_encontrado}')

    def _montar_observacoes(self, ocorrencia):
        partes = []
        for texto in (
            ocorrencia.observacoes_adicionais,
            ocorrencia.motivo_alegado,
            ocorrencia.alegado_responsavel,
        ):
            texto = (texto or '').strip()
            if texto:
                partes.append(texto)
        return '\n'.join(partes)

    def _colunas_falta_existentes(self):
        with connection.cursor() as cursor:
            return {
                coluna.name
                for coluna in connection.introspection.get_table_description(
                    cursor,
                    RegistroFaltaAluno._meta.db_table,
                )
            }

    def _inserir_faltas(self, faltas_para_criar):
        colunas_existentes = self._colunas_falta_existentes()
        colunas = [
            coluna
            for coluna in (
                'aluno_id',
                'data',
                'quantidade_faltas',
                'justificada',
                'responsavel_contatado',
                'observacoes',
                'pedagoga',
                'registrado_por_id',
                'registrado_em',
            )
            if coluna in colunas_existentes
        ]
        placeholders = ', '.join(['%s'] * len(colunas))
        nomes_colunas = ', '.join(connection.ops.quote_name(coluna) for coluna in colunas)
        sql = f'INSERT INTO {connection.ops.quote_name(RegistroFaltaAluno._meta.db_table)} ({nomes_colunas}) VALUES ({placeholders})'
        valores = [tuple(falta[coluna] for coluna in colunas) for falta in faltas_para_criar]

        with connection.cursor() as cursor:
            cursor.executemany(sql, valores)
