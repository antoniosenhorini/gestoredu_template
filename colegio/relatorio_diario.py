#!/usr/bin/env python
import os
import sys
from pathlib import Path
import django
import pandas as pd
from io import BytesIO
from datetime import datetime, timedelta
from django.core.mail import EmailMessage
from django.template.loader import render_to_string

# Configurar ambiente Django (para rodar como script externo)
sys.path.append(str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'colegio.settings')
django.setup()

from apps.models import RegistroOcorrenciaAluno
from django.utils import timezone

def enviar_relatorio(data=None, email_destino='antonio.senhorini@escola.pr.gov.br'):
    if data is None:
        data = timezone.now().date() - timedelta(days=1)  # ontem

    ocorrencias = RegistroOcorrenciaAluno.objects.filter(data=data).select_related(
        'aluno', 'aluno__turma', 'registrado_por'
    )

    if not ocorrencias:
        print(f"Nenhuma ocorrência em {data}. Nenhum e-mail enviado.")
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

    assunto = f'Relatório de Ocorrências - {data.strftime("%d/%m/%Y")}'
    corpo = f"""
    <html>
    <body>
    <h2>Relatório Diário - {data.strftime("%d/%m/%Y")}</h2>
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
        from_email='sistema@colegio.com',  # será substituído pelo DEFAULT_FROM_EMAIL
        to=[email_destino],
    )
    email.content_subtype = 'html'
    email.attach(
        f'relatorio_ocorrencias_{data.strftime("%Y%m%d")}.xlsx',
        output.getvalue(),
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    email.send()
    print(f"Relatório de {data} enviado para {email_destino}")

if __name__ == "__main__":
    # Se quiser testar para uma data específica: python relatorio_diario.py 2026-05-01
    if len(sys.argv) > 1:
        data = datetime.strptime(sys.argv[1], '%Y-%m-%d').date()
        enviar_relatorio(data=data)
    else:
        enviar_relatorio()
