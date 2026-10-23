import openpyxl
from django.shortcuts import render, redirect
from django.contrib import messages
from apps.models import Aluno, Turma

def importar_alunos(request):
    if request.method == 'POST' and request.FILES.get('arquivo'):
        excel_file = request.FILES['arquivo']
        
        try:
            wb = openpyxl.load_workbook(excel_file)
            sheet = wb.active
            count = 0
            
            # Pula a primeira linha (cabeçalho)
            for row in sheet.iter_rows(min_row=2, values_only=True):
                if not row or not row[5]:
                    continue

                serie = str(row[0]).strip() if row[0] else ""
                turma_letra = str(row[1]).strip() if row[1] else ""
                turno_excel = str(row[2]).strip() if row[2] else ""
                numero = row[3]
                nome = str(row[5]).strip()
                
                telefone = str(row[6]).strip() if row[6] else ""
                curso = str(row[9]).strip() if row[9] else ""
                
                nome_turma = f"{serie} {turma_letra}".strip()
                
                # Tratando o Turno (O sistema só aceita 'manha' ou 'tarde')
                turno_sistema = None
                if turno_excel.lower() == 'manhã' or turno_excel.lower() == 'manha':
                    turno_sistema = 'manha'
                elif turno_excel.lower() == 'tarde':
                    turno_sistema = 'tarde'
                # Se for "Integral", deixamos None para não quebrar o banco

                # Cria a turma com todos os campos obrigatórios
                turma_obj, _ = Turma.objects.get_or_create(
                    nome=nome_turma,
                    defaults={
                        'ano': 2026,           # Obrigatório (Integer)
                        'serie': serie,        # Obrigatório (String)
                        'turno': turno_sistema,
                        'ativa': True
                    }
                )
                
                Aluno.objects.update_or_create(
                    turma=turma_obj,
                    numero=numero,
                    defaults={
                        'nome': nome, 
                        'ativo': True,
                        'telefone': telefone,
                        'curso': curso
                    }
                )
                count += 1
                
            messages.success(request, f'{count} alunos importados com sucesso!')
            return redirect('listar_alunos')
            
        except Exception as e:
            messages.error(request, f'Erro ao importar: {e}')
            
    return render(request, 'importar_alunos.html')