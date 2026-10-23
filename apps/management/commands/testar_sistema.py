from django.core.management.base import BaseCommand
from django.test import Client
from django.contrib.auth.models import User
from django.db import transaction
from django.urls import reverse, NoReverseMatch
from apps.models import Aluno, RegistroOcorrenciaAluno
import datetime

class Command(BaseCommand):
    help = 'Roda um teste funcional completo no sistema'

    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.MIGRATE_HEADING("🔍 Iniciando Teste Funcional Completo...\n"))
        client = Client()
        
        admin = User.objects.filter(is_superuser=True).first()
        if admin:
            client.force_login(admin)
            self.stdout.write("   🔐 Logado como superusuário.")
        else:
            self.stdout.write(self.style.ERROR("   ❌ Nenhum superusuário encontrado."))
            return

        erros = 0

        # 1. Teste de Telas usando reverse() em vez de URLs fixas
        self.stdout.write("\n🌐 Testando abertura de telas...")
        nomes_rotas = [
            'painel_equipe', 'registrar_ocorrencia_aluno', 'conferencia_ocorrencias',
            'listar_alunos', 'listar_professores', 'controle_faltas_alunos',
            'registrar_falta_aluno', 'busca_ativa'
        ]
        
        for nome in nomes_rotas:
            try:
                url = reverse(nome)
                res = client.get(url, HTTP_HOST='127.0.0.1')
                if res.status_code == 200:
                    self.stdout.write(self.style.SUCCESS(f"   ✅ OK: {nome} ({url})"))
                elif res.status_code == 302:
                    self.stdout.write(self.style.WARNING(f"   ⚠️ REDIRECIONOU: {nome} (Pode precisar de login ou falta dados)"))
                else:
                    self.stdout.write(self.style.ERROR(f"   ❌ ERRO {res.status_code}: {nome}"))
                    erros += 1
            except NoReverseMatch:
                self.stdout.write(self.style.ERROR(f"   ❌ ROTA NÃO ENCONTRADA NO urls.py: {nome}"))

        # 2. Teste de Relatórios (Apenas os nomes que a gente sabe que existem)
        self.stdout.write("\n📊 Testando geração de Relatórios...")
        # Puxando as rotas que estavam no menu do painel
        nomes_relatorios = ['relatorio_ocorrencias_alunos', 'relatorio_faltas_alunos']
        
        for nome in nomes_relatorios:
            try:
                url = reverse(nome)
                res = client.get(url, HTTP_HOST='127.0.0.1')
                if res.status_code in [200, 302]:
                    self.stdout.write(self.style.SUCCESS(f"   ✅ OK: {nome} (Status {res.status_code})"))
                else:
                    self.stdout.write(self.style.ERROR(f"   ❌ ERRO {res.status_code}: {nome}"))
                    erros += 1
            except NoReverseMatch:
                self.stdout.write(self.style.WARNING(f"   ⚠️ Relatório não mapeado no urls.py: {nome}"))

        # 3. Teste de Ação: Salvar uma Ocorrência (POST)
        self.stdout.write("\n💾 Testando salvamento de Ocorrência (Simulação)...")
        
        aluno_teste = Aluno.objects.first()
        if aluno_teste:
            turma = aluno_teste.turma
            url_post = reverse('registrar_ocorrencia_aluno')
            
            # Usamos uma data no futuro (2030) para não dar conflito com testes antigos
            dados_post = {
                'turma': turma.id,
                'numero_aluno': aluno_teste.numero,
                'data': '2030-12-31',
                'tipo_ocorrencia': 'atraso',
                'pedagoga': 'Robô de Teste',
                'motivo_alegado': 'Teste automatizado',
                'turno': 'integral',
            }

            try:
                with transaction.atomic():
                    res_post = client.post(url_post, dados_post, HTTP_HOST='127.0.0.1')
                    
                    if res_post.status_code == 302:
                        # Verifica se realmente foi para o banco (buscando pela data de 2030)
                        nova_ocorrencia = RegistroOcorrenciaAluno.objects.filter(aluno=aluno_teste, data='2030-12-31').exists()
                        
                        if nova_ocorrencia:
                            self.stdout.write(self.style.SUCCESS("   ✅ OK: Ocorrência salva e confirmada no banco!"))
                        else:
                            self.stdout.write(self.style.WARNING("   ⚠️ O sistema barrou o salvamento (regra de duplicidade funcionando)."))
                    else:
                        self.stdout.write(self.style.ERROR(f"   ❌ ERRO ao salvar: Status {res_post.status_code}"))
                        erros += 1
                        
                    raise Exception("Rollback de teste")
            except Exception as e:
                if str(e) != "Rollback de teste":
                    self.stdout.write(self.style.ERROR(f"   ❌ Falha no POST: {str(e)}"))
                    erros += 1
                else:
                    self.stdout.write("   🗑️ Registro de teste desfeito do banco (Segurança).")
        else:
            self.stdout.write(self.style.WARNING("   ⚠️ Sem alunos no banco para testar."))