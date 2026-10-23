import openpyxl
from django.shortcuts import render, redirect
from django.contrib import messages
from apps.models import Professor

def importar_professores(request):
    if request.method == 'POST' and request.FILES.get('arquivo'):
        excel_file = request.FILES['arquivo']
        
        try:
            wb = openpyxl.load_workbook(excel_file)
            sheet = wb.active
            count = 0
            
            for row in sheet.iter_rows(min_row=2, values_only=True):
                if not row or not row[0]:
                    continue

                # Como mudamos o Excel, as colunas agora são:
                # A=0 (Nome), B=1 (Disciplina/Cargo), C=2 (Telefone), D=3 (Email)
                nome = str(row[0]).strip()
                disciplina = str(row[1]).strip() if row[1] else ""
                telefone = str(row[2]).strip() if row[2] else ""
                email = str(row[3]).strip() if row[3] else ""

                Professor.objects.update_or_create(
                    nome_completo=nome,
                    defaults={
                        'disciplinas': disciplina, # Salva direto!
                        'telefone': telefone,
                        'email': email,
                        'ativo': True
                    }
                )
                count += 1
                
            messages.success(request, f'{count} professores importados com sucesso!')
            return redirect('listar_professores')
            
        except Exception as e:
            messages.error(request, f'Erro ao importar: {e}')
            
    return render(request, 'importar_professores.html')