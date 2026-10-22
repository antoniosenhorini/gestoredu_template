# apps/management/commands/enviar_relatorio_diario.py

import pandas as pd
from io import BytesIO
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand
from django.core.mail import EmailMessage
from django.utils import timezone
from apps.models import RegistroOcorrenciaAluno

class Command(BaseCommand):
    help = 'Gera e envia relatório diário de ocorrências'

    def add_arguments(self, parser):
        parser.add_argument('--data', type=str, help='Data no formato YYYY-MM-DD')
        parser.add_argument('--email', type=str, default='antonio.senhorini@escola.pr.gov.br', help='E-mail de destino')

    def handle(self, *args, **options):
        if options['data']:
            data = datetime.strptime(options['data'], '%Y-%m-%d').date()
        else:
            data = timezone.now().date() - timedelta(days=1)

        ocorrencias = RegistroOcorrenciaAluno.objects.filter(data=data).select_related(
            'aluno', 'aluno__turma', 'registrado_por'
        )

        if not ocorrencias:
            self.stdout.write(self.style.WARNING(f'Nenhuma ocorrência em {data}. Nenhum relatório gerado.'))
            return

        dados = []
        for occ in ocorrencias:
            digitador = occ.registrado_por.username if occ.registrado_por else "Sistema"
            dados.append({
                'Data': occ.data.strftime('%d/%m/%Y'),
                'Turma': occ.aluno.turma.nome,
                'Nº': occ.aluno.numero,
                'Aluno': occ.aluno.nome,
                'Tipo': occ.get_tipo_ocorrencia_display(),
                'Turno': occ.turno,
                'Digitador': digitador,
                'Hora Reg.': occ.registrado_em.strftime('%H:%M:%S') if occ.registrado_em else '-'
            })

        df = pd.DataFrame(dados)
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Ocorrências')
            worksheet = writer.sheets['Ocorrências']
            for col in worksheet.columns:
                max_len = max(len(str(cell.value)) for cell in col) + 2
                worksheet.column_dimensions[col[0].column_letter].width = min(max_len, 50)

        output.seek(0)

        total_faltas = len(df[df['Tipo'] == 'Falta'])
        total_atrasos = len(df[df['Tipo'] == 'Atraso'])

        assunto = f'Relatório Diário - {data.strftime("%d/%m/%Y")}'
        corpo = f"""
        <html>
        <body>
        <h2>Relatório de Ocorrências - {data.strftime("%d/%m/%Y")}</h2>
        <p><strong>Faltas:</strong> {total_faltas}</p>
        <p><strong>Atrasos:</strong> {total_atrasos}</p>
        <p>Em anexo o detalhamento completo.</p>
        <hr>
        <small>Sistema automático - Colégio Cívico Militar</small>
        </body>
        </html>
        """

        email = EmailMessage(
            subject=assunto,
            body=corpo,
            to=[options['email']],
        )
        email.content_subtype = 'html'
        email.attach(
            f'relatorio_ocorrencias_{data.strftime("%Y%m%d")}.xlsx',
            output.getvalue(),
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        email.send()

        self.stdout.write(self.style.SUCCESS(f'Relatório de {data} enviado para {options["email"]}'))